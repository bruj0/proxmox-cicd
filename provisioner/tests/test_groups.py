"""test_groups — WP2 regression tests for the groups abstraction.

WP2 of the GroupSpec plan introduces a second seam: a
`BaseGroup` ABC that wraps a DAG of apps. This is the
first half of the WP2–WP4 acceptance block (the second
half — the orchestrator wiring + CLI flag — is WP3 + WP4).

These tests pin five invariants for the package itself:

  1. `BaseGroup` is an abstract class; concrete subclasses
     must declare `name`, `nodes`, `edges`, `enabled_in`.
  2. `DefaultGroup` is a sentinel: empty nodes, always
     `enabled_in`, the orchestrator's "no group = every
     enabled app in catalog order" catch-all.
  3. `CicdStackGroup` declares the four nodes + three
     edges from §5.1, and `enabled_in` gates on
     `gitea` being in the catalog.
  4. `topological_order(group)` produces a stable
     order; a cycle raises `CyclicGroupError` with the
     offending path.
  5. The registry (`@register_group`) behaves like
     `@register`: rejects non-BaseGroup, rejects empty
     names, refuses duplicate names from different
     modules.

These tests deliberately do NOT exercise the
orchestrator or CLI (those land in WP3 / WP4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provisioner.lib.catalog import (
    AppConfig,
    Catalog,
    CatalogError,
)


SHIPPED_YAML = Path("provisioner/lib/catalog/shipped.yaml")


def _build_catalog(enabled: list[str]) -> Catalog:
    """Build a minimal in-memory Catalog with the given
    apps enabled. Lets us exercise `enabled_in` /
    topological order without writing YAML to disk.
    """
    apps = {
        name: AppConfig(enabled=(name in enabled), extra={}, values={})
        for name in (
            "gitea",
            "gitea-runner",
            "cloudflared",
            "vaultwarden-k8s-sync",
        )
    }
    return Catalog(
        cluster_name="test-cluster",
        apps=apps,
        ingress_base_domain="example.net",
        source_path=None,
    )


# ----------------------------------------------------------- registry mechanics


def test_default_group_is_registered() -> None:
    """`DefaultGroup` is registered under the name `default`.

    The orchestrator (WP3) defaults `--group` to `default`;
    if `DefaultGroup` isn't registered at import time,
    the orchestrator will fail with a confusing
    `unknown group` error instead of the intended
    "use the sentinel" path.
    """
    from provisioner.lib.groups import all_groups, group_by_name

    groups = {g.name for g in all_groups()}
    assert "default" in groups
    assert group_by_name("default") is not None


def test_cicd_stack_group_has_expected_nodes_and_edges() -> None:
    """`CicdStackGroup` declares the four nodes + three
    edges from §5.1 of the plan.

    The edge shape is the whole point of the
    `GroupSpec` (DAG) abstraction: gitea has no deps,
    vaultwarden-k8s-sync depends on gitea, and both
    gitea-runner and cloudflared depend on
    vaultwarden-k8s-sync. This DAG cannot be expressed
    as a flat list, which is exactly the regression
    risk this test pins.
    """
    from provisioner.lib.groups import group_by_name

    cls = group_by_name("cicd-stack")
    assert cls is not None
    group = cls()
    nodes = group.nodes
    assert set(nodes.keys()) == {
        "gitea",
        "gitea-runner",
        "cloudflared",
        "vaultwarden-k8s-sync",
    }
    edges = group.edges
    # gitea has no deps; gitea-runner + cloudflared both
    # wait for VKS.
    assert edges["gitea"] == []
    assert edges["vaultwarden-k8s-sync"] == ["gitea"]
    assert edges["gitea-runner"] == ["vaultwarden-k8s-sync"]
    assert edges["cloudflared"] == ["vaultwarden-k8s-sync"]


def test_group_enabled_in_returns_false_when_prereq_missing() -> None:
    """`CicdStackGroup.enabled_in(catalog)` is `False`
    when `gitea` is missing from the catalog.

    `gitea` is the root of the cicd-stack DAG; without
    it the stack cannot run (VKS depends on gitea's
    namespace, runners depend on VKS, etc.). This is
    the explicit gate described in §5.1.
    """
    from provisioner.lib.groups import group_by_name

    cls = group_by_name("cicd-stack")
    assert cls is not None
    group = cls()

    # gitea enabled -> group runs.
    assert group.enabled_in(_build_catalog(["gitea"])) is True

    # gitea missing -> group refuses.
    catalog_no_gitea = _build_catalog(
        ["gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    assert group.enabled_in(catalog_no_gitea) is False


def test_resolve_apply_order_topologically_sorts_dag() -> None:
    """The orchestrator's `resolve_apply_order` produces
    a stable topological order from a group's DAG.

    For `cicd-stack`: gitea must come first, then
    vaultwarden-k8s-sync, then gitea-runner +
    cloudflared (in either order, since they share a
    parent). This is the §5.1 contract.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    order = resolve_apply_order(catalog, "cicd-stack")

    assert order.index("gitea") == 0
    assert order.index("vaultwarden-k8s-sync") == 1
    # gitea-runner + cloudflared must come AFTER
    # vaultwarden-k8s-sync but their relative order is
    # unspecified (DAG, not list).
    assert order.index("gitea-runner") > order.index(
        "vaultwarden-k8s-sync"
    )
    assert order.index("cloudflared") > order.index(
        "vaultwarden-k8s-sync"
    )


def test_resolve_apply_order_raises_when_group_app_not_enabled() -> None:
    """`resolve_apply_order` raises `CatalogError` when
    the catalog has `vaultwarden-k8s-sync: enabled: false`
    (or any group node disabled).

    The plan's WP2-WP4 acceptance block calls this out
    explicitly: `cicdctl apply cicd --group cicd-stack`
    exits with EXIT_CATALOG when a group node is
    disabled in the cluster catalog.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(["gitea", "gitea-runner", "cloudflared"])
    # vaultwarden-k8s-sync intentionally missing.
    with pytest.raises(CatalogError, match="vaultwarden-k8s-sync"):
        resolve_apply_order(catalog, "cicd-stack")


def test_resolve_apply_order_intersects_group_with_app_filter() -> None:
    """When an `--app` filter is supplied alongside a
    group, the result is the intersection of the
    group's topological order and the filter.

    This is the §5.6 mutual-exclusion rule:
    `--group + --app` is legal; the group defines the
    candidate set, `--app` narrows it.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    order = resolve_apply_order(
        catalog, "cicd-stack", app_filter=["gitea", "cloudflared"]
    )

    # Only the filter apps survive, and they remain
    # in topological order relative to each other.
    assert order == ["gitea", "cloudflared"]


def test_resolve_apply_order_raises_on_cycle_with_cycle_path() -> None:
    """A group whose DAG contains a cycle raises
    `CyclicGroupError` with the offending cycle path
    in the error message.

    The plan's §5.1 says: "A cycle produces
    `CyclicGroupError` (a `CatalogError` subclass) with
    the offending cycle path; the orchestrator catches
    it and exits with EXIT_CATALOG."
    """
    from provisioner.lib.groups import (
        BaseGroup,
        CyclicGroupError,
        resolve_apply_order,
    )

    # Build a cycle: a -> b -> a.
    class CycleGroup(BaseGroup):
        name = "cycle-group"

        @property
        def nodes(self):  # type: ignore[override]
            return {"a": "", "b": ""}

        @property
        def edges(self):  # type: ignore[override]
            return {"a": ["b"], "b": ["a"]}

        def enabled_in(self, catalog):  # type: ignore[override]
            return True

    # Cycle test: catalog enables the cycle nodes so
    # the resolver reaches the topological sort (the
    # "missing apps" gate is upstream and not what this
    # test exercises).
    apps = {
        "a": AppConfig(enabled=True, extra={}, values={}),
        "b": AppConfig(enabled=True, extra={}, values={}),
    }
    catalog = Catalog(
        cluster_name="test-cluster",
        apps=apps,
        ingress_base_domain="example.net",
        source_path=None,
    )
    with pytest.raises(CyclicGroupError) as ei:
        resolve_apply_order(catalog, "cycle-group", _group=CycleGroup())
    # The cycle path contains both a and b.
    assert "a" in str(ei.value)
    assert "b" in str(ei.value)


def test_destroy_order_is_reverse_topological_of_group() -> None:
    """Destroy order is the reverse of apply order.

    Teardown must undo dependencies in the opposite
    order they were built: gitea-runner / cloudflared
    first (they came last), then vaultwarden-k8s-sync,
    then gitea. This mirrors today's `destroy` behaviour
    (reverse catalog order) but is now a property of
    the group DAG, not a hard-coded list.
    """
    from provisioner.lib.groups import (
        resolve_apply_order,
        resolve_destroy_order,
    )

    catalog = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    apply_order = resolve_apply_order(catalog, "cicd-stack")
    destroy_order = resolve_destroy_order(catalog, "cicd-stack")

    # Destroy is the reverse of apply.
    assert destroy_order == list(reversed(apply_order))
    # gitea is destroyed last.
    assert destroy_order[-1] == "gitea"
    # vaultwarden-k8s-sync is destroyed before gitea.
    assert destroy_order.index("vaultwarden-k8s-sync") < destroy_order.index(
        "gitea"
    )


def test_default_group_resolves_to_catalog_order() -> None:
    """`DefaultGroup` is the sentinel "every enabled app,
    in catalog order". `resolve_apply_order(catalog,
    "default")` must return `catalog.enabled_app_names()`
    verbatim (no edges applied).
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    assert resolve_apply_order(catalog, "default") == (
        catalog.enabled_app_names()
    )


def test_register_group_rejects_non_basegroup_class() -> None:
    """`@register_group` rejects a class that doesn't
    subclass `BaseGroup`. Mirrors `@register` for apps.
    """
    from provisioner.lib.groups import register_group

    class NotAGroup:
        name = "not-a-group"

    with pytest.raises(TypeError, match="must subclass BaseGroup"):
        register_group(NotAGroup)  # type: ignore[arg-type]


def test_register_group_rejects_duplicate_name() -> None:
    """`@register_group` refuses two groups with the same
    `name` from different modules (same logical group
    from the same module is OK; tests rely on that).
    """
    from provisioner.lib.groups import BaseGroup, register_group

    class GroupA(BaseGroup):
        name = "dup-group"

        @property
        def nodes(self):  # type: ignore[override]
            return {}

        @property
        def edges(self):  # type: ignore[override]
            return {}

        def enabled_in(self, catalog):  # type: ignore[override]
            return True

    class GroupB(BaseGroup):
        name = "dup-group"

        @property
        def nodes(self):  # type: ignore[override]
            return {}

        @property
        def edges(self):  # type: ignore[override]
            return {}

        def enabled_in(self, catalog):  # type: ignore[override]
            return True

    register_group(GroupA)
    with pytest.raises(ValueError, match="dup-group"):
        register_group(GroupB)


def test_unknown_group_raises_catalog_error() -> None:
    """`resolve_apply_order(catalog, "nope")` raises
    `CatalogError` so the orchestrator can translate it
    to EXIT_CATALOG.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(["gitea"])
    with pytest.raises(CatalogError, match="nope"):
        resolve_apply_order(catalog, "nope")
