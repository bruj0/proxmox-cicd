"""Small helper for resolving the kubeconfig path used to
read the in-cluster ``vaultwarden-kubernetes-secrets``
Secret (BW_CLIENTID + BW_CLIENTSECRET).

Resolution order, matching the orchestrator's kubectl
runner convention and ``scripts/reseed-vks-creds.sh``:

  1. explicit ``kubeconfig`` argument
  2. ``$KUBECONFIG`` environment variable
  3. ``~/.kube/config`` (the kubectl default)
  4. ``<sibling-proxmox-k3s-repo>/infra/clusters/cicd/kubeconfig.yaml``
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_kubeconfig(
    explicit: str | None,
    sibling_repo: str | None = None,
) -> str:
    """Return the kubeconfig path to use.

    Args:
      explicit: the path passed via ``--kubeconfig``.
      sibling_repo: override path to the sibling
        ``proxmox-k3s`` repo. Defaults to ``../proxmox-k3s``
        relative to the cwd.
    """
    if explicit:
        return explicit
    env = os.environ.get("KUBECONFIG")
    if env:
        return env
    default = Path.home() / ".kube" / "config"
    if default.exists():
        return str(default)
    here = Path.cwd().resolve()
    fallback_root = (
        Path(sibling_repo).resolve() if sibling_repo
        else here.parent / "proxmox-k3s"
    )
    fallback = fallback_root / "infra" / "clusters" / "cicd" / "kubeconfig.yaml"
    return str(fallback)
