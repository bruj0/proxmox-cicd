"""groups/cicd_stack — CicdStackGroup.

The four-app stack that ships with proxmox-cicd:

    vaultwarden-k8s-sync        (root — everyone else writes Secrets through it)
        |
        +--> gitea              (stores admin password in VKS as a Secure Note)
        |       |
        |       +--> gitea-runner   (registration token via VKS; uses
        |                          gitea-admin-secret at install time)
        |
        +--> cloudflared        (tunnel token via VKS)

The DAG is the regression risk this group pins: a
linear sequence can't express "gitea-runner waits
for both VKS and gitea, but cloudflared only waits
for VKS", which is why WP0–WP4 introduce the
`GroupSpec` abstraction in the first place.

`enabled_in` gates on `vaultwarden-k8s-sync` being in
the catalog — VKS is the root of the DAG, so without
it the rest of the stack cannot apply. The
orchestrator translates the closed gate into a
"group requires vaultwarden-k8s-sync" message in plan
output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import register_group
from .base import BaseGroup

if TYPE_CHECKING:
    from ..catalog import Catalog


@register_group
class CicdStackGroup(BaseGroup):
    name = "cicd-stack"
    description = "Provision a cicd cluster end-to-end."

    @property
    def nodes(self) -> dict[str, str]:
        return {
            "vaultwarden-k8s-sync": (
                "root; everyone else writes Secrets through it"
            ),
            "gitea": (
                "stores admin password in VKS as a Secure Note"
            ),
            "gitea-runner": (
                "needs VKS to populate the registration-token "
                "Secret; also depends on gitea being ready"
            ),
            "cloudflared": (
                "needs VKS for the tunnel-token Secret"
            ),
        }

    @property
    def edges(self) -> dict[str, list[str]]:
        return {
            # vaultwarden-k8s-sync has no dependencies;
            # runs first. Every other app reconciles
            # Secrets through VKS, so VKS must be up
            # before any of them can apply.
            "vaultwarden-k8s-sync": [],
            "gitea": ["vaultwarden-k8s-sync"],
            # gitea-runner waits for VKS (the
            # registration-token Secret) AND for gitea
            # (the runner registers against gitea on
            # startup). cloudflared only needs VKS — its
            # tunnel token is independent of gitea.
            "gitea-runner": [
                "vaultwarden-k8s-sync",
                "gitea",
            ],
            "cloudflared": ["vaultwarden-k8s-sync"],
        }

    def enabled_in(self, catalog: Catalog) -> bool:
        # VKS is the root of the DAG; without it the
        # stack can't run.
        return (
            "vaultwarden-k8s-sync" in catalog.enabled_app_names()
        )
