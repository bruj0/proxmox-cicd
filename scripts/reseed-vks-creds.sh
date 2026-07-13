#!/usr/bin/env bash
# scripts/reseed-vks-creds.sh
#
# Re-seed the VKS auth Secret in the vaultwarden-kubernetes-secrets
# namespace with a fresh client_id + client_secret + master_password
# triple. Use this after creating a new Vaultwarden account (without
# 2FA — VKS does not support 2FA) and generating a new user API key
# in the Vaultwarden web UI.
#
# Usage:
#   ./scripts/reseed-vks-creds.sh
#
# Reads CLIENT_ID / CLIENT_SECRET from the local .env file and
# prompts for the master password on the terminal (read -s). Then
# patches the existing Secret in-place and rolls the Deployment so
# the new pod picks up the new env at start (env vars are baked
# in at container start; a Secret patch alone won't re-read them).
#
# After the script finishes, watch the first sync cycle with:
#   kubectl -n vaultwarden-kubernetes-secrets \
#     logs -l app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f
#
# "Vault unlocked successfully" -> the sync is polling.

set -euo pipefail

NAMESPACE=vaultwarden-kubernetes-secrets
SECRET_NAME=vaultwarden-kubernetes-secrets
# Resolve the cluster's kubeconfig. Resolution order:
#   1. $KUBECONFIG (already set in the operator's env)
#   2. $KUBECONFIG_PATH (legacy alias this script
#      historically used)
#   3. kubectl's default lookup path ($HOME/.kube/config)
#   4. the proxmox-k3s sibling repo's per-cluster path
#      (relative to the proxmox-cicd repo root, so the
#      script works regardless of where the operator
#      cloned the repo)
#
# We do NOT hardcode the operator's home path. Override
# KUBECONFIG or KUBECONFIG_PATH in the calling shell to
# point at a different cluster's kubeconfig.
KUBECONFIG_PATH="${KUBECONFIG:-${KUBECONFIG_PATH:-}}"
if [[ -z "$KUBECONFIG_PATH" ]]; then
  if [[ -f "$HOME/.kube/config" ]]; then
    KUBECONFIG_PATH="$HOME/.kube/config"
  else
    # Sibling proxmox-k3s repo, expected to be a peer
    # of the proxmox-cicd checkout.
    _here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    KUBECONFIG_PATH="$_here/../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml"
  fi
fi
export KUBECONFIG="$KUBECONFIG_PATH"

# Load CLIENT_ID + CLIENT_SECRET from .env if present.
# .env format: KEY=VALUE, one per line. We accept both the
# Vaultwarden UI names (client_id/client_secret) and the VKS
# names (BW_CLIENTID/BW_CLIENTSECRET). The script is
# defensive about leading/trailing whitespace + quoting
# (some tools export a "key= value" with a space, or
# "key='value'" with quotes).
if [[ -f .env ]]; then
  while IFS='=' read -r key rest; do
    # Skip blanks + comments.
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    key="${key// /}"
    # Strip optional surrounding quotes + leading whitespace
    # from the value side.
    value="${rest#"${rest%%[![:space:]]*}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    case "$key" in
      client_id|BW_CLIENTID) CLIENT_ID="$value" ;;
      client_secret|BW_CLIENTSECRET) CLIENT_SECRET="$value" ;;
    esac
  done < .env
fi

CLIENT_ID=${CLIENT_ID:-}
CLIENT_SECRET=${CLIENT_SECRET:-}

if [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "ERROR: CLIENT_ID and CLIENT_SECRET must be set." >&2
  echo "  either add them to .env as BW_CLIENTID=... / BW_CLIENTSECRET=..." >&2
  echo "  or as client_id=... / client_secret=..." >&2
  exit 1
fi

read -s -p "Vaultwarden master password: " MASTER_PASSWORD
echo

echo ">> Patching Secret $NAMESPACE/$SECRET_NAME ..."
kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --from-literal=BW_CLIENTID="$CLIENT_ID" \
  --from-literal=BW_CLIENTSECRET="$CLIENT_SECRET" \
  --from-literal=VAULTWARDEN__MASTERPASSWORD="$MASTER_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">> Rolling the Deployment so the new pod re-reads the Secret ..."
kubectl -n "$NAMESPACE" rollout restart deployment/vaultwarden-kubernetes-secrets

echo ">> Waiting for the rollout to complete (max 60s) ..."
kubectl -n "$NAMESPACE" rollout status deployment/vaultwarden-kubernetes-secrets --timeout=60s

echo ">> Latest log lines:"
kubectl -n "$NAMESPACE" logs \
  -l app.kubernetes.io/name=vaultwarden-kubernetes-secrets --tail=5

echo
echo "Done. Watch the next sync with:"
echo "  kubectl -n $NAMESPACE logs -l app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f"
