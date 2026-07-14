# Cloudflare Tunnel — provisioning, secret flow, and rotation

The single source of truth for the `cloudflared` app:
how the tunnel is minted, where its token lives, how
the cluster Secret gets populated, and how to rotate
the token end-to-end. Companion docs cover the
individual moving parts in detail (linked at the
bottom).

## TL;DR

- The Cloudflare Tunnel is a **remotely-managed**
  Cloudflare Tunnel named `cicd-tunnel` on the
  operator's Cloudflare account. The Cloudflare
  control plane owns its config (ingress rules,
  DNS records); cloudflared on the cluster just
  connects to it.
- The orchestrator mints the tunnel + a scoped
  Cloudflare API token, persists both to host-side
  cache files, and **seeds the base64 tunnel token
  into Vaultwarden as a Secure Note** so
  [VaultwardenK8sSync][vks] (VKS) can recreate the
  chart-managed `Secret/cloudflare-tunnel-remote`
  if helm ever deletes it.
- VKS writes that secret to
  `Secret/cloudflared/cloudflare-tunnel-remote`,
  key `tunnelToken`. The cloudflared Deployment
  mounts it as `$TUNNEL_TOKEN`.
- The chart's Secret has its helm-emitted labels
  and annotations **stripped by a post-renderer** so
  VKS and helm don't fight over `managed-by`. See
  [cloudflared-helm-post-renderer.md][postr].

[vks]: ./vaultwarden-sync.md
[postr]: ./cloudflared-helm-post-renderer.md

## End-to-end data flow

```mermaid
flowchart LR
  CF[Cloudflare API<br/>api.cloudflare.com]
  ORCH[cloudflared app<br/>apply step]
  VT[Vaultwarden<br/>secrets@bruj0.net]
  VKS[VaultwardenK8sSync<br/>Deployment<br/>polls every ~30s]
  CHART[cloudflare-tunnel-remote<br/>chart 0.1.2]
  PR[post-renderer<br/>strip_helm_secret_labels]
  SECRET[Secret cloudflared/<br/>cloudflare-tunnel-remote]
  POD[cloudflared pod<br/>$TUNNEL_TOKEN]

  CF -- "POST /accounts/:id/cfd_tunnel<br/>+ scoped API token<br/>+ PUT ingress" --> ORCH
  ORCH -- "tunnel_token (base64)" --> CACHE1["infra/secrets/<br/>cloudflared-tunnel.json"]
  ORCH -- "POST /api/ciphers<br/>name=cloudflared k8s secret value<br/>body=tunnel_token" --> VT
  VT -- "GET /api/sync<br/>every ~30s" --> VKS
  VKS -- "create / update Secret<br/>key=tunnelToken" --> SECRET
  ORCH -- "helm upgrade --install" --> CHART
  CHART -- "rendered YAML" --> PR
  PR -- "strips helm labels<br/>from the Secret" --> SECRET
  SECRET -- "env TUNNEL_TOKEN" --> POD
  POD -- "outbound to<br/>&lt;uuid&gt;.cfargotunnel.com" --> CF
```

### What the orchestrator owns (host side)

| File | Mode | Purpose | Lifetime |
| --- | --- | --- | --- |
| `infra/secrets/cloudflared-tunnel.json` | 0600 | Tunnel record: `{id, name, tunnel_token (base64), credentials_file}`. | Reused on every apply; rotated only when Cloudflare no longer recognises the cached `id`. |
| `infra/secrets/cloudflared-api-token.json` | 0600 | Scoped Cloudflare API token (`Account:Cloudflare Tunnel:Edit` + `Zone:DNS:Edit`). | Minted once via the global API key, then cached. |

Both files are gitignored. The orchestrator never
persists `CLOUDFLARE_GLOBAL_API_KEY` — it's used
exactly once to mint the scoped token.

### What the orchestrator owns (Vaultwarden side)

The orchestrator seeds **one** Vaultwarden Secure
Note. After the 2026-07-14 audit (which found 4
duplicate copies from a missing idempotency guard),
the seed path lists first and only POSTs when no
cipher with the same VKS triple exists. The audit
log line on a fresh apply is
`cloudflared.vws_seed_ok`; on a no-op apply it is
`cloudflared.vws_seed_skipped`.

| Cipher field | Value |
| --- | --- |
| Display name (decrypted) | `cloudflared k8s secret value` |
| Body (decrypted) | The base64 `tunnel_token` |
| Custom field `namespaces` | `cloudflared` |
| Custom field `secret-name` | `cloudflare-tunnel-remote` |
| Custom field `secret-key` | `tunnelToken` |

The custom-field triple is the **primary key** VKS
uses to route the body to the right cluster Secret.
One cipher with the canonical triple is the
intended state; any extra ciphers with the same
triple are noise — see the rotation runbook below
for cleanup.

### What VKS owns (cluster side)

VKS picks up the note on its next ~30s cycle and
writes (or updates) the cluster Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-tunnel-remote
  namespace: cloudflared
  labels:
    app.kubernetes.io/managed-by: vaultwarden-kubernetes-secrets
    app.kubernetes.io/created-by:  vaultwarden-k8s-sync
    app.kubernetes.io/instance:    cloudflare-tunnel-remote
    app.kubernetes.io/name:        cloudflare-tunnel-remote
    app.kubernetes.io/version:     latest
  annotations:
    vaultwarden-kubernetes-secrets/content-hash: "<sha256>"
    vaultwarden-kubernetes-secrets/managed-keys:  '["tunnelToken"]'
stringData:
  tunnelToken: "<base64>"
```

The cloudflared Deployment mounts `stringData.tunnelToken`
as `env[0].valueFrom.secretKeyRef.key: tunnelToken`,
so the running pod sees the latest VKS-written value
within ~30s of the orchestrator pushing the note.

### What helm owns

The chart owns the Deployment, ServiceAccount, and
(other) cluster resources — those pass through the
post-renderer untouched. It does **not** own the
Secret's labels or annotations (the post-renderer
strips them); it also doesn't own the Secret's
data (VKS writes that on every sync). Helm's only
contract with the Secret is: "ensure the resource
exists at the named Secret; if it does, leave the
labels alone". See [cloudflared-helm-post-renderer.md][postr]
for why this split exists.

## Field-manager contract — the short version

Two controllers writing labels on the same Secret
is a kubectl field-manager race. The post-renderer
breaks the race by ensuring **helm never asserts a
label** on the Secret — so VKS is the sole owner of
`metadata.labels` for that Secret, and `helm upgrade`
succeeds on every re-run.

Full design + the exact label keys stripped,
test coverage, and the `kubectl --server-side`
behaviour that motivates the fix:
[cloudflared-helm-post-renderer.md][postr].

## Rotation runbook

Two distinct cases. Pick the one that matches your
situation.

### Routine rotation (token compromised or expired)

This rotates the tunnel secret without changing the
Cloudflare tunnel identity. The tunnel UUID stays
the same; only the bearer token changes. Cloudflare
generates a new token for an existing tunnel
server-side on `DELETE` + `POST /cfd_tunnel`
rotation; the orchestrator's `_ensure_tunnel` does
this automatically when it detects the cache is
gone.

```sh
# 1. Delete the host-side cache so the orchestrator
#    is forced through the rotate path on next apply.
rm -i infra/secrets/cloudflared-tunnel.json

# 2. Delete the Vaultwarden note so VKS doesn't keep
#    rewriting the cluster Secret with the old token.
#    The orchestrator re-seeds on the next apply.
uv run vaultwarden-notes --password-file /tmp/vw.pw \
  delete --match "cloudflared k8s secret value" --yes

# 3. Re-apply. The orchestrator detects the missing
#    cache, calls list_by_name → finds the tunnel on
#    Cloudflare → rotate (delete + mint under the
#    same name) → persists the new base64 token →
#    seeds a fresh Vaultwarden note.
uv run cicdctl apply cicd

# 4. Watch VKS pick up the new note (≤30s).
kubectl -n vaultwarden-kubernetes-secrets logs \
  -l app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f

# 5. Confirm the cloudflared pod reconnected to the
#    new tunnel.
kubectl -n cloudflared logs \
  -l app.kubernetes.io/name=cloudflare-tunnel-remote -f
# Expect "Registered tunnel connection" within 30s.
```

### Hard rotation (tunnel UUID must change)

Reserved for the case where the **tunnel identity**
itself must change — e.g. the account moves
between Cloudflare orgs, or a future audit policy
mandates tunnel rotation. The Cloudflare API does
not expose a "rotate UUID" operation; you mint a
new tunnel under a new name and update the DNS
record to point at the new tunnel.

```sh
# 1. Pick a new tunnel name. Keep the slug ASCII
#    and DNS-safe; it becomes part of the
#    `<uuid>.cfargotunnel.com` hostname.
NEW_NAME="cicd-tunnel-v2"

# 2. Wipe host-side state so the orchestrator
#    creates fresh.
rm -i infra/secrets/cloudflared-tunnel.json
rm -i infra/secrets/cloudflared-api-token.json

# 3. Edit the orchestrator's TUNNEL_NAME constant
#    (provisioner/lib/apps/cloudflared.py) to the
#    new name. Pin a code review on this change —
#    it's a one-line code edit, but the DNS record
#    is keyed on it.

# 4. Wipe the Vaultwarden note so VKS doesn't
#    reapply the old Secret.
uv run vaultwarden-notes --password-file /tmp/vw.pw \
  delete --match "cloudflared k8s secret value" --yes

# 5. Apply. The orchestrator mints a fresh tunnel
#    under NEW_NAME, creates a new DNS record,
#    seeds a new Vaultwarden note.
uv run cicdctl apply cicd

# 6. (Manual) After verifying the new tunnel works,
#    delete the old one from the Cloudflare
#    dashboard → Zero Trust → Networks → Tunnels.
```

### Recovering from a lost `/tmp/vw.pw`

The master password file is required for
`vaultwarden-notes` and the orchestrator's seed
step. If it's gone but the cluster is still up
(the tunnel is currently connected), rotation is
**not** urgent — VKS keeps the cluster Secret
alive from the existing Vaultwarden note, and helm
owns the rest. Re-create the file at your leisure
(see `docs/runbooks/setup-vaultwarden-sync.md`).

If `/tmp/vw.pw` is lost **and** the cluster Secret
is missing, the orchestrator's `_seed_vaultwarden_note`
exits non-fatally (`vws_seed_no_password_file`
warning), helm still owns the Secret via
`values/cloudflared-tunnel-remote.values-rendered.yaml`,
and cloudflared keeps running. To restore the VKS
loop, recreate `/tmp/vw.pw` (mode 0600) and re-run
`uv run cicdctl apply cicd`.

### Cleaning up duplicate Vaultwarden notes

If the orchestrator's idempotency guard was
disabled or your vault predates it, you may have
multiple `cloudflared k8s secret value` entries
with identical bodies. VKS still picks the right
Secret (it merges on the triple), but it's noise
that drifts on web-UI edits. Find and prune:

```sh
# 1. List all cloudflared entries.
uv run vaultwarden-notes --password-file /tmp/vw.pw list \
  | grep "cloudflared k8s secret value"

# 2. Decrypt one of them to confirm it carries the
#    canonical triple (namespaces=cloudflared,
#    secret-name=cloudflare-tunnel-remote,
#    secret-key=tunnelToken).
uv run vaultwarden-notes --password-file /tmp/vw.pw \
  decrypt --id <cipher-id>

# 3. Delete all but the freshest revisionDate.
#    (The most-recently-edited cipher is the live
#    one — VKS's content-hash matches its body.)
uv run vaultwarden-notes --password-file /tmp/vw.pw \
  delete --id <stale-id> --yes
```

The audit log lists all VKS writes with
`vaultwarden-kubernetes-secrets/content-hash`; that
hash matches exactly one cipher body, so you can
cross-reference if the canonical one is ambiguous.

## Files at a glance

| Concern | File | Owner | Documented in |
| --- | --- | --- | --- |
| Tunnel + token lifecycle | `provisioner/lib/apps/cloudflared.py` | orchestrator | this doc |
| CloudflareTunnelClient (REST helpers) | `provisioner/lib/apps/cloudflared_tunnel.py` | orchestrator | this doc |
| Vaultwarden client (auth, cipher CRUD) | `provisioner/lib/vaultwarden/` | orchestrator | [vaultwarden-notes.md](./vaultwarden-notes.md) |
| Vaultwarden sync target | VaultwardenK8sSync chart | upstream + values | [vaultwarden-sync.md](./vaultwarden-sync.md) |
| Helm/VKS post-renderer | `provisioner/lib/helm_post_renderers/strip_helm_secret_labels.py` | orchestrator | [cloudflared-helm-post-renderer.md](./cloudflared-helm-post-renderer.md) |
| Test suite | `provisioner/tests/test_cloudflared.py` + `test_cloudflared_tunnel.py` + `test_vaultwarden_client.py` + `test_helm_post_renderer.py` | — | inline docstrings |

## See also

- [cloudflared-helm-post-renderer.md](./cloudflared-helm-post-renderer.md) —
  the post-renderer design that breaks the helm ↔ VKS
  field-manager race. Required reading before changing
  the Secret's labels.
- [vaultwarden-notes.md](./vaultwarden-notes.md) —
  the `VaultwardenClient` library and the
  `vaultwarden-notes` CLI used by the orchestrator's
  seed step. The CLI is the operator's tool for
  inspecting the vault (e.g. finding duplicates
  during cleanup).
- [vaultwarden-sync.md](./vaultwarden-sync.md) —
  VKS architecture, the chart's `custom_fields`
  contract, and the broader "how does Vaultwarden
  flow into cluster Secrets" question.
- [architecture.md § S — Single Responsibility](./architecture.md#s--single-responsibility) —
  the SOLID design constraints that motivated
  splitting the Cloudflare logic into
  `cloudflared.py` + `cloudflared_tunnel.py` +
  `vaultwarden/` + `helm_post_renderers/`.
- [runbooks/setup-vaultwarden-sync.md](./runbooks/setup-vaultwarden-sync.md) —
  one-time VKS setup including the
  `/tmp/vw.pw` master password contract.
