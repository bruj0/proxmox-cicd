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
    """`CicdStackGroup` declares the four nodes + four
    edges from §5.1 of the plan (VKS-rooted DAG).

    The DAG shape is the whole point of the
    `GroupSpec` abstraction:

      vaultwarden-k8s-sync     (root — no deps)
          |
          +--> gitea                   (after VKS)
          |
          +--> cloudflared             (after VKS, sibling of gitea)
          |
          +--> gitea-runner            (after VKS AND after gitea)

    A linear sequence can't express "gitea-runner
    waits for gitea but cloudflared doesn't" — the
    DAG can. VKS is the root because gitea's apply()
    writes the admin password to a Vaultwarden Secure
    Note that VKS reconciles into a cluster Secret.
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
    # VKS is the root; everyone else waits for it.
    # gitea-runner additionally waits for gitea (the
    # runner registers against gitea on startup);
    # cloudflared only needs VKS.
    assert edges["vaultwarden-k8s-sync"] == []
    assert edges["gitea"] == ["vaultwarden-k8s-sync"]
    assert sorted(edges["gitea-runner"]) == [
        "gitea",
        "vaultwarden-k8s-sync",
    ]
    assert edges["cloudflared"] == ["vaultwarden-k8s-sync"]


def test_group_enabled_in_returns_false_when_prereq_missing() -> None:
    """`CicdStackGroup.enabled_in(catalog)` is `False`
    when `vaultwarden-k8s-sync` is missing from the
    catalog.

    VKS is the root of the cicd-stack DAG; without
    it the stack cannot run (gitea stores its admin
    password in VKS, the runner + cloudflared rely
    on VKS-reconciled Secrets). This is the explicit
    gate described in §5.1.
    """
    from provisioner.lib.groups import group_by_name

    cls = group_by_name("cicd-stack")
    assert cls is not None
    group = cls()

    # VKS enabled -> group runs.
    assert (
        group.enabled_in(
            _build_catalog(["vaultwarden-k8s-sync"])
        )
        is True
    )

    # VKS missing -> group refuses.
    catalog_no_vks = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared"]
    )
    assert group.enabled_in(catalog_no_vks) is False


def test_resolve_apply_order_topologically_sorts_dag() -> None:
    """The orchestrator's `resolve_apply_order` produces
    a stable topological order from a group's DAG.

    For `cicd-stack` (VKS-rooted):

      1. vaultwarden-k8s-sync   (root; no deps)
      2. gitea, cloudflared     (siblings; both wait for VKS only)
      3. gitea-runner           (waits for VKS AND gitea)

    This is the §5.1 contract.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(
        ["gitea", "gitea-runner", "cloudflared", "vaultwarden-k8s-sync"]
    )
    order = resolve_apply_order(catalog, "cicd-stack")

    assert order.index("vaultwarden-k8s-sync") == 0
    # gitea and cloudflared are siblings — both come
    # after VKS but their relative order is
    # unspecified (DAG, not list).
    assert order.index("gitea") > order.index(
        "vaultwarden-k8s-sync"
    )
    assert order.index("cloudflared") > order.index(
        "vaultwarden-k8s-sync"
    )
    # gitea-runner is last: it depends on both VKS and
    # gitea.
    assert order.index("gitea-runner") > order.index("gitea")
    assert order.index("gitea-runner") > order.index(
        "vaultwarden-k8s-sync"
    )
    # Specifically: gitea-runner comes after gitea AND
    # after cloudflared, since both are resolved before
    # the runner can apply.
    assert order[-1] == "gitea-runner"


def test_resolve_apply_order_raises_when_group_app_not_enabled() -> None:
    """`resolve_apply_order` raises `CatalogError` when
    the catalog has any group node disabled.

    The plan's WP2-WP4 acceptance block calls this out
    explicitly: `cicdctl apply cicd --group cicd-stack`
    exits with EXIT_CATALOG when a group node is
    disabled in the cluster catalog.
    """
    from provisioner.lib.groups import resolve_apply_order

    catalog = _build_catalog(
        ["gitea", "cloudflared", "vaultwarden-k8s-sync"]
    )
    # gitea-runner intentionally missing.
    with pytest.raises(CatalogError, match="gitea-runner"):
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

    # VKS is filtered out by the app_filter; gitea and
    # cloudflared are siblings in the DAG (both wait
    # only for VKS), so their relative order is the
    # alphabetical tiebreak the resolver produces. Both
    # orderings satisfy the §5.1 contract; the test
    # pins the concrete one to guard against silent
    # resolver regressions.
    assert order == ["cloudflared", "gitea"]
    # And the test confirms VKS is not in the filtered
    # output.
    assert "vaultwarden-k8s-sync" not in order
    assert "gitea-runner" not in order


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
    order they were built: gitea-runner first (it came
    last in apply), then gitea + cloudflared (siblings),
    then VKS (the root, destroyed last because removing
    it last keeps Secrets available for the longest
    possible window during teardown). This mirrors
    today's `destroy` behaviour (reverse catalog order)
    but is now a property of the group DAG, not a
    hard-coded list.
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
    # gitea-runner is destroyed first (it was applied last).
    assert destroy_order[0] == "gitea-runner"
    # VKS is destroyed last (the root).
    assert destroy_order[-1] == "vaultwarden-k8s-sync"
    # VKS's index in destroy < VKS's index in apply.
    #


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
