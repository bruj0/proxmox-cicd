# Runbook: rotate the Gitea admin password via Bitwarden

The Gitea chart's `gitea.admin.passwordMode:
initialOnlyRequireReset` setting means the bootstrap creates
the admin user with a one-time password. The operator must
change it on first login, and from then on it's stored in
Gitea's own database — not in Bitwarden, not in the helm
values.

This runbook covers rotating secrets that ARE managed by
Bitwarden: the gitea-runner registration token.

## Background

The Gitea Runner uses an ephemeral registration token
(`GITEA_RUNNER_REGISTRATION_TOKEN`) that's valid for as
long as the Gitea admin doesn't reset it. When the admin
resets it:

1. The BitwardenSecret CR (`gitea-runner-registration`) is
   stale.
2. The bitwarden-sm-operator re-syncs every 5 minutes
   (set in `values/bitwarden-sm-operator.yaml`).
3. The new token lands in the `gitea-runner-config` k8s
   Secret.
4. The runner picks it up on its next poll.

## The recipe

### 1. Reset the token in Gitea's web UI

1. Sign in to Gitea as `gitea_admin`.
2. Go to Site Administration -> Actions -> Runners ->
   Create new runner. Click "Reset token" on an existing
   runner if you just want to rotate.
3. Copy the new token.

### 2. Update the Bitwarden Secrets Manager secret

Either via the web UI or via the Bitwarden CLI:

```bash
# Using the Bitwarden CLI (bw) authenticated to your org.
export BW_SESSION="$(bw unlock --raw)"
bw edit item <secret-uuid> \
  notes="<new-registration-token>"
bw sync
```

The `bw edit` command updates the secret in-place. The
BitwardenSecret CR syncs within `bwSecretsManagerRefreshInterval`
seconds (default 300).

### 3. Watch the sync

```bash
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  -n gitea-runner get bitwardensecret gitea-runner-registration \
  -o jsonpath='{.status.conditions[*].message}'
# -> "Secret synced successfully"

# Confirm the k8s Secret was rotated:
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  -n gitea-runner get secret gitea-runner-config \
  -o jsonpath='{.data.registrationToken}' | base64 -d
# -> <new-token>
```

### 4. Bounce the runner pod

```bash
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  -n gitea-runner rollout restart deployment gitea-runner-gitea-runner
```

(Deployment name = `<release>-<chart-name>` = `gitea-runner-
gitea-runner` per the chart's _helpers.tpl.)

The runner pod restarts, reads the new token from the Secret,
and re-registers against Gitea. Because the runner is
`ephemeral: true`, each new poll uses the current token.

## What if Bitwarden is down?

If the BitwardenSecrets Manager API is unreachable, the
BitwardenSecret CR's status will be `Synced=False, Reason=
SyncFailed`. The runner pod will keep using the last-known
token until the API recovers. This is fine — the old token
is still valid for as long as Gitea hasn't reset it.

## What about the Gitea `INTERNAL_TOKEN`?

The chart generates a random `INTERNAL_TOKEN` on first install
and stores it in a k8s Secret (`gitea-internal-token`). It's
used for Gitea's internal API auth. If you need to rotate it,
delete the Secret and let the chart recreate it:

```bash
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  -n gitea delete secret gitea-internal-token
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  -n gitea rollout restart statefulset/gitea-postgresql
# (the chart re-reads the secret on the next pod restart)
```

(This requires running `cicdctl apply` afterwards if the
chart re-creates the Secret with a new value, otherwise
the Gitea pods will fail to start.)

For production, it's better to either:
- Add `gitea.config.security.INTERNAL_TOKEN` to
  `values/gitea.yaml` and let helm manage it, or
- Wire a `BitwardenSecret` CR to source it from Bitwarden.

Neither is in scope for v0.1.0.