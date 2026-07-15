"""groups/default — DefaultGroup sentinel.

The `DefaultGroup` is the orchestrator's "no group = every
enabled app in catalog order" catch-all (§5.5). Its
`nodes` property returns an empty dict; the resolver
treats that as the sentinel and falls through to
`catalog.enabled_app_names()`.

`enabled_in` always returns `True` because there's no
precondition for "do exactly what the catalog says".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import register_group
from .base import BaseGroup

if TYPE_CHECKING:
    from ..catalog import Catalog


@register_group
class DefaultGroup(BaseGroup):
    name = "default"
    description = (
        "Every enabled app, in catalog order (today's behaviour)."
    )

    @property
    def nodes(self) -> dict[str, str]:
        # Sentinel: empty nodes -> the resolver fills
        # from `catalog.enabled_app_names()`.
        return {}

    @property
    def edges(self) -> dict[str, list[str]]:
        # No edges when nodes is empty (the resolver
        # never consults this property for the default
        # group).
        return {}

    def enabled_in(self, catalog: Catalog) -> bool:  # noqa: ARG002
        # Always-on: the default group is "do whatever
        # the catalog says" — no preconditions.
        return True
