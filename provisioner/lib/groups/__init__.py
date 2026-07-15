"""groups — BaseGroup ABC + @register_group + resolvers.

Mirrors `apps/__init__.py`. A group is a named DAG of
apps; the orchestrator topologically sorts the DAG
and applies each app in turn. Cycles raise
`CyclicGroupError`.

The default group `default` has empty `nodes` and is a
sentinel the resolver treats as "every enabled app,
in catalog order".

Force-imports at the bottom of this module populate
the registry at package-import time. The orchestrator
imports this package once and discovers every shipped
group via `all_groups()`.
"""

from __future__ import annotations

import graphlib
from typing import TYPE_CHECKING

from ..catalog import Catalog, CatalogError
from .base import BaseGroup, CyclicGroupError

if TYPE_CHECKING:
    pass


# ----- registry -----


_REGISTRY: dict[str, type[BaseGroup]] = {}


def register_group(cls: type[BaseGroup]) -> type[BaseGroup]:
    """Decorator: register `cls` in the global group
    registry.

    Groups import this from `provisioner.lib.groups`
    and decorate their `BaseGroup` subclass. The
    orchestrator pulls them back out via `all_groups()`.

    Mirrors `@register` for apps:

      1. `cls` must subclass `BaseGroup`.
      2. `name` must be a non-empty string.
      3. Two groups with the same name from different
         modules raise `ValueError`; same module +
         qualname are treated as the same logical
         group (so test re-imports are safe).
    """
    if not isinstance(cls, type) or not issubclass(cls, BaseGroup):
        raise TypeError(
            f"{cls.__name__ if isinstance(cls, type) else cls!r} "
            f"must subclass BaseGroup to be @register_group'ed."
        )
    name = getattr(cls, "name", None)
    if not name:
        raise TypeError(
            f"{cls.__name__} must define a non-empty `name` "
            f"class attr to be @register_group'ed."
        )
    if name in _REGISTRY:
        existing = _REGISTRY[name]
        if (existing.__module__, existing.__qualname__) != (
            cls.__module__,
            cls.__qualname__,
        ):
            raise ValueError(
                f"group name '{name}' already registered to "
                f"{existing.__name__}"
            )
    _REGISTRY[name] = cls
    return cls


def all_groups() -> tuple[type[BaseGroup], ...]:
    """Return every registered BaseGroup subclass, in
    registration order.
    """
    return tuple(_REGISTRY.values())


def group_by_name(name: str) -> type[BaseGroup] | None:
    """Look up a single group class by its registered
    name. Returns `None` if not found; callers usually
    raise `CatalogError` on `None` so the orchestrator
    can exit with `EXIT_CATALOG`.
    """
    return _REGISTRY.get(name)


def reset_registry() -> None:
    """Clear the registry. Used by tests to isolate
    side effects.
    """
    _REGISTRY.clear()


# ----- resolvers -----


def _resolve_group(name: str) -> type[BaseGroup]:
    """Look up a group by name or raise `CatalogError`.

    Centralises the "unknown group -> CatalogError"
    translation so every caller (orchestrator, tests,
    CLI) gets the same error shape.
    """
    cls = group_by_name(name)
    if cls is None:
        raise CatalogError(
            f"unknown group '{name}'. Known groups: "
            f"{sorted(_REGISTRY)}"
        )
    return cls


def _build_topo(
    group: BaseGroup, catalog: Catalog
) -> list[str]:
    """Run `graphlib.TopologicalSorter` over `group`'s
    DAG and return a stable topological order.

    Cycle detection: we walk the DAG with a
    colour-tagged DFS to extract the offending cycle
    path (e.g. `a -> b -> a`) so the error message
    is actionable. `graphlib.CycleError` doesn't
    expose the path on all Python versions, so we
    don't rely on it.

    `catalog.enabled_app_names()` is intersected with
    the DAG nodes so disabled apps never enter the
    ordering. An app that's in the group but disabled
    in the catalog raises `CatalogError` (the
    `cicdctl apply cicd --group cicd-stack` exit
    case from the WP2-WP4 acceptance block).
    """
    enabled = set(catalog.enabled_app_names())
    nodes = group.nodes

    # Group node that isn't enabled in the catalog is
    # a hard error: the operator asked for a group
    # whose prereq is missing. We name the missing app
    # in the error so the message is actionable.
    missing = sorted(set(nodes) - enabled)
    if missing:
        raise CatalogError(
            f"group {group.name!r} requires app(s) "
            f"{missing} but the cluster catalog does "
            f"not enable them. Enable them in "
            f"infra/clusters/{catalog.cluster_name}/"
            f"catalog.yaml or pick a different group."
        )

    edges = group.edges
    # Build the subgraph of enabled nodes + their deps.
    subgraph: dict[str, list[str]] = {}
    for node in sorted(enabled):
        deps = edges.get(node, [])
        subgraph[node] = [d for d in deps if d in enabled]

    # DFS for a cycle. Returns the cycle path as a
    # list of node names if one exists, else None.
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in subgraph}
    parent: dict[str, str | None] = {n: None for n in subgraph}

    def _dfs(start: str) -> list[str] | None:
        colour[start] = GREY
        for nxt in subgraph.get(start, []):
            if colour[nxt] == GREY:
                # Cycle: walk parent pointers from
                # start back to nxt.
                cycle = [nxt, start]
                node = parent[start]
                while node is not None and node != nxt:
                    cycle.append(node)
                    node = parent[node]
                cycle.append(nxt)
                cycle.reverse()
                return cycle
            if colour[nxt] == WHITE:
                parent[nxt] = start
                found = _dfs(nxt)
                if found is not None:
                    return found
        colour[start] = BLACK
        return None

    for node in sorted(subgraph):
        if colour[node] == WHITE:
            cycle = _dfs(node)
            if cycle is not None:
                raise CyclicGroupError(
                    f"group {group.name!r} contains a "
                    f"cycle: {' -> '.join(cycle)}"
                )

    sorter: graphlib.TopologicalSorter[str] = (
        graphlib.TopologicalSorter()
    )
    for node in sorted(enabled):
        sorter.add(node, *subgraph[node])

    return list(sorter.static_order())


def resolve_apply_order(
    catalog: Catalog,
    group_name: str,
    app_filter: list[str] | None = None,
    *,
    _group: BaseGroup | None = None,
) -> list[str]:
    """Return the apply order for `catalog` under
    `group_name`, optionally narrowed by `app_filter`.

    The order is:

      1. Look up the group (or instantiate the
         `_group` override — used by tests that
         inject a cycle-bearing group without
         registering it).
      2. Topologically sort the DAG against the
         catalog's enabled apps.
      3. Intersect with `app_filter` (preserving
         topological order) if a filter is supplied.

    Empty `nodes` (the `DefaultGroup` sentinel) falls
    through to `catalog.enabled_app_names()` with no
    topological sort applied — today's behaviour.
    """
    if _group is None:
        cls = _resolve_group(group_name)
        group = cls()
    else:
        group = _group

    if not group.enabled_in(catalog):
        raise CatalogError(
            f"group {group.name!r} is not enabled for "
            f"cluster {catalog.cluster_name!r}. Check the "
            f"group's `enabled_in` gate (e.g. cicd-stack "
            f"requires gitea to be enabled)."
        )

    # DefaultGroup sentinel: empty nodes -> catalog order.
    if not group.nodes:
        order = catalog.enabled_app_names()
    else:
        order = _build_topo(group, catalog)

    if app_filter is not None:
        # Intersect, preserving topological order.
        filter_set = set(app_filter)
        order = [n for n in order if n in filter_set]

    return order


def resolve_destroy_order(
    catalog: Catalog,
    group_name: str,
    app_filter: list[str] | None = None,
) -> list[str]:
    """Return the destroy order for `catalog` under
    `group_name` — the reverse of `resolve_apply_order`.

    Teardown undoes dependencies in the opposite
    order they were built: gitea-runner / cloudflared
    first (they came last), then vaultwarden-k8s-sync,
    then gitea.
    """
    apply_order = resolve_apply_order(
        catalog, group_name, app_filter
    )
    return list(reversed(apply_order))


# Force-import shipped groups at package-import time
# so the registry is populated before the orchestrator
# asks for `all_groups()`. Each module applies
# `@register_group` as a side effect at class
# definition time, so importing the modules is what
# populates the registry.
from . import cicd_stack as _cicd_stack  # noqa: E402, F401
from . import default as _default  # noqa: E402, F401


__all__ = [
    "BaseGroup",
    "CyclicGroupError",
    "all_groups",
    "group_by_name",
    "register_group",
    "reset_registry",
    "resolve_apply_order",
    "resolve_destroy_order",
]
