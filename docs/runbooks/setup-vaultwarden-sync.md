# Runbook: setup Vaultwarden → k8s secret sync (VKS)

This runbook walks through enabling the
`vaultwarden-k8s-sync` (VKS) app on the `cicd` cluster,
including the one-time Vaultwarden account creation and
the per-install credential seed.

## Background

VKS is a polling service that reads items from a Vaultwarden
(or Bitwarden-compatible) server and writes them as
Kubernetes Secrets. It replaces the previous
`bitwarden-sm-operator` chart (CRD-based, Bitwarden
Secrets Manager API) with a simpler model: the
service talks to the Bitwarden public API directly.

The contract is:

- **Item type**: any (Secure Note, Login, Card, …).
- **Custom field `namespaces`**: comma-separated list of
  target k8s namespaces. VKS will sync the item into
  each.
- **Custom fields `secret-name` / `secret-key-password` /
  `secret-key-username` / `secret-key`**: optional
  overrides for the resulting k8s Secret's name and
  data keys. Default name = sanitized item name, default
  data key = `password` (Secure Notes use `notes`).
- **Lifecycle**: `SYNC__DELETEORPHANS=true` (set in
  `values/vaultwarden-kubernetes-secrets.yaml`) makes
  VKS delete k8s Secrets when their source item is
  removed from Vaultwarden.

## Limitations (VKS upstream)

- **No 2FA support** — VKS does not handle the
  Vaultwarden 2FA challenge during vault unlock
  (the .NET client has no `2fa_token` flow). Accounts
  used for VKS must have 2FA disabled.
- **PBKDF2 / Argon2id KDF** — supported; the sync
  service derives the symmetric key with the master
  password the same way the official Bitwarden
  clients do.
- **API key auth** — VKS uses a user API key
  (`Settings → Account → API Key` in the web UI) +
  master password. Org API keys are not supported.

## One-time setup (per Vaultwarden instance)

### 1. Create a dedicated sync account

If you don't already have a no-2FA Vaultwarden user
for VKS, create one at your Vaultwarden URL
(the placeholder in `infra/clusters/<name>/catalog.yaml`
is `https://bitwarden.example.net` — replace with
your real URL):

1. Sign up with a fresh email. **Do not enable 2FA.**
2. Note the master password — you'll type it into
   the terminal during the seed step (it's not
   stored in `.env`).
3. Generate a user API key: `Settings → Account →
   API Key → View API Key`. Copy both `client_id`
   (UUID) and `client_secret`.

### 2. Add the credentials to `.env`

In the `proxmox-cicd/` repo root, create or update
`.env` (gitignored, mode 0600):

```ini
# .env — operator-local credentials. NEVER COMMIT.
# Recognized key names (case-insensitive):
#   client_id  / BW_CLIENTID
#   client_secret / BW_CLIENTSECRET
#   master_password / VAULTWARDEN_MASTERPASSWORD
#                   / VAULTWARDEN__MASTERPASSWORD
client_id=user.<your-uuid>
client_secret=<your-client-secret>
master_password=<your-master-password>
```

The provisioner's `vaultwarden_k8s_sync` app reads
`.env` on every `cicdctl apply cicd` run and auto-seeds
the auth Secret. The master password is the one piece
of info you may want to type interactively instead
of saving to disk — in that case, leave it out of
`.env` and the apply will emit a manual seed step
in its next-step output.

### 3. Verify the app is enabled in the catalog

`infra/clusters/cicd/catalog.yaml` should already have
`vaultwarden-k8s-sync.enabled: true` after the
provisioner swap landed. If not, add it:

```yaml
apps:
  vaultwarden-k8s-sync:
    enabled: true
```

## Per-install flow

### 1. Apply

```sh
# from the proxmox-cicd repo root
uv run cicdctl apply cicd --auto-approve
```

The orchestrator:
- Installs the VKS helm chart (2.0.0 from
  `oci://ghcr.io/antoniolago/charts/...`).
- Reads `.env` and seeds the auth Secret
  (`vaultwarden-kubernetes-secrets` namespace) with
  `BW_CLIENTID`, `BW_CLIENTSECRET`, and
  `VAULTWARDEN__MASTERPASSWORD`.
- Rolls the Deployment so the new pod reads the
  populated env at start.

### 2. Verify the first sync

```sh
KUBECONFIG=../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  kubectl -n vaultwarden-kubernetes-secrets \
  logs -l app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f
```

Look for:

- `API key login successful` (proves the
  `client_id` / `client_secret` work).
- `Vault unlocked successfully` (proves the master
  password is correct for the user that owns the
  API key).
- `⭕ COMPLETED - NO CHANGES` or
  `✅ Synced N items` (proves the polling loop is
  running).

### 3. Smoke test

Create a Secure Note in Vaultwarden with:

- **Name**: `vks-smoke-test`
- **Notes**: any string
- **Custom field `namespaces`**: `default`

Within 30s the sync should create
`default/vks-smoke-test` Secret:

```sh
KUBECONFIG=../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  kubectl -n default get secret vks-smoke-test -o yaml
```

Cleanup: delete the item in Vaultwarden and VKS
will garbage-collect the k8s Secret on the next
cycle (`SYNC__DELETEORPHANS=true`).

## Rotating credentials

To rotate the API key, master password, or both:

```sh
# Update .env with the new values, then:
./scripts/reseed-vks-creds.sh
```

The script reads `client_id` + `client_secret` from
`.env`, prompts for the master password (read -s,
not echoed), patches the auth Secret, and rolls the
Deployment. Watch the next sync with the same `kubectl
logs` command as in step 2.

If you've rotated the **master password**, VKS must
re-decrypt the symmetric key with the new password.
The first sync cycle after the rotation may show
`Vault unlock (key derivation) failed` if the
password didn't match. Re-run the script with the
correct password.

## Why a separate no-2FA account?

VKS's `VaultwardenService.UnlockVaultAsync` doesn't
handle the `2fa_token` challenge that Bitwarden
returns when 2FA is enabled on the user account.
The .NET client expects the master password alone
to derive the symmetric key. With 2FA, the API key
authenticates the user but the unlock step fails
with `Failed to decrypt symmetric key (got 0 bytes)`.
A dedicated no-2FA service account is the only way
to run VKS without patching the upstream.

## Where the values live

| File | Role |
| --- | --- |
| `infra/clusters/cicd/catalog.yaml` | enables the app for the cluster |
| `values/vaultwarden-kubernetes-secrets.yaml` | chart values (server URL, log level, sync interval) |
| `versions.yaml` | pin chart 2.0.0 / appVersion 2.0.0 |
| `.env` (gitignored) | operator-local client_id / client_secret / master_password |
| `infra/secrets/...` (gitignored) | not used; VKS lives entirely in-cluster |
