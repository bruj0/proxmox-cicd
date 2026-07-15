"""groups/cicd_stack — CicdStackGroup.

The four-app stack that ships with proxmox-cicd:

    gitea                       (root)
        |
        v
    vaultwarden-k8s-sync        (after gitea)
        |
        +--> gitea-runner       (after VKS)
        +--> cloudflared        (after VKS)

The DAG is the regression risk this group pins: a
linear sequence can't express "gitea-runner and
cloudflared don't depend on each other", which is why
WP0–WP4 introduce the `GroupSpec` abstraction in the
first place.

`enabled_in` gates on `gitea` being in the catalog —
`gitea` is the root of the DAG, so without it the
rest of the stack cannot apply. The orchestrator
translates the closed gate into a "group requires
gitea" message in plan output.
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
            "gitea": (
                "applies first; everything else depends "
                "on its namespace"
            ),
            "vaultwarden-k8s-sync": (
                "reconciles Secrets from Vaultwarden"
            ),
            "gitea-runner": (
                "needs VKS to populate the "
                "registration-token Secret"
            ),
            "cloudflared": (
                "needs VKS for the tunnel-token Secret"
            ),
        }

    @property
    def edges(self) -> dict[str, list[str]]:
        return {
            # gitea has no dependencies; runs first.
            "gitea": [],
            "vaultwarden-k8s-sync": ["gitea"],
            "gitea-runner": ["vaultwarden-k8s-sync"],
            "cloudflared": ["vaultwarden-k8s-sync"],
        }

    def enabled_in(self, catalog: Catalog) -> bool:
        # gitea is the root of the DAG; without it the
        # stack can't run.
        return "gitea" in catalog.enabled_app_names()
