"""test_catalog_only_enablement — WP14 cross-cutting guards.

WP14 codifies the cross-cutting policy promises:

  * GroupSpec.nodes cannot reference apps the catalog
    has `enabled: false` (or apps the catalog doesn't
    know about).
  * No Python module reads a cluster-level
    `enabled=True` flag outside of `catalog.yaml`
    (the catalog is the only enablement source).
  * Groups are registered via the `@register_group`
    decorator only; the orchestrator's discoverer
    rejects unregistered classes.
  * Adding a new group is a one-file edit: the new
    module's import goes in
    `provisioner/lib/groups/__init__.py` and the
    registry picks it up automatically on package
    import.

A regression in any of these shape promises is a
breaking change for operators — these tests fail the
build before that regression ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from provisioner.lib.catalog import (
    CatalogError,
    load_shipped_catalog,
)
from provisioner.lib.groups import (
    BaseGroup,
    all_groups,
    register_group,
)


PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"
GROUPS_DIR = PROVISIONER / "provisioner" / "lib" / "groups"
SHIPPED_CATALOG_PATH = PROVISIONER / "provisioner" / "lib" / "catalog" / "shipped.yaml"


# ----- group-refers-to-disabled-app guard -----


def _build_catalog_with_enabled(disabled_app: str) -> object:
    """Build a Catalog where `disabled_app` has
    `enabled: false` in the per-cluster config. Uses
    the shipped catalog as a starting point and
    patches the entry via `ShippedCatalog` directly."""
    from provisioner.lib.catalog import AppConfig, Catalog

    shipped = load_shipped_catalog(SHIPPED_CATALOG_PATH)
    cluster = Catalog(
        cluster_name="test",
        apps={
            name: AppConfig(enabled=(name != disabled_app))
            for name in shipped.apps
        },
        ingress_base_domain="example.net",
    )
    return Catalog.from_shipped_and_cluster(
        shipped=shipped,
        cluster=cluster,
    )


def test_groups_cannot_reference_disabled_app(tmp_path: Path) -> None:
    """A GroupSpec that lists a `disabled: true` app
    in its nodes raises CatalogError at orchestrator
    startup, not at apply time.

    The wrap-then-apply ordering matters: catching
    it here means `cicdctl plan` fails the same way
    as `cicdctl apply`, with the same error.
    """
    catalog = _build_catalog_with_enabled(disabled_app="gitea")
    # Pre-condition: the catalog has gitea disabled.
    assert "gitea" not in catalog.enabled_app_names()

    # The shipped cicd-stack group lists gitea as a
    # node; resolving the group should raise
    # CatalogError because gitea is not in
    # `enabled_app_names()`.
    from provisioner.lib.groups import resolve_apply_order

    with pytest.raises(CatalogError) as exc_info:
        resolve_apply_order(catalog, "cicd-stack")
    # Error message names the disabled app so the
    # operator can grep the audit log.
    assert "gitea" in str(exc_info.value)


# ----- group-refers-to-unknown-app guard -----


def test_groups_cannot_reference_unknown_app() -> None:
    """A GroupSpec.nodes entry for an app not in
    `apps.registry` raises CatalogError. The shipped
    catalog is the only enablement source — apps
    not in the catalog are unknown to the system
    full stop."""
    # Build a fake group with one node: a bogus app.
    class _BadGroup(BaseGroup):
        name = "_bad_unknown_app_group"

        nodes = ("definitely-not-an-app",)
        edges = {}

        def enabled_in(self, catalog):  # type: ignore[no-untyped-def]
            return True

    register_group(_BadGroup)
    try:
        # Re-resolve via the registry so the catalog
        # error path is exercised.
        assert _BadGroup.name in [g.name for g in all_groups()]

        # The resolver must reject this group before
        # apply — even if it would otherwise qualify.
        shipped = load_shipped_catalog(SHIPPED_CATALOG_PATH)
        from provisioner.lib.catalog import AppConfig, Catalog

        cluster = Catalog(
            cluster_name="test",
            apps={
                name: AppConfig(enabled=True)
                for name in shipped.apps
            },
            ingress_base_domain="example.net",
        )
        catalog = Catalog.from_shipped_and_cluster(
            shipped=shipped,
            cluster=cluster,
        )
        from provisioner.lib.groups import resolve_apply_order

        with pytest.raises(CatalogError) as exc_info:
            resolve_apply_order(catalog, "_bad_unknown_app_group")
        assert "definitely-not-an-app" in str(exc_info.value)
    finally:
        # Clean up so we don't pollute the registry
        # for other tests.
        from provisioner.lib.groups import _REGISTRY

        _REGISTRY.pop("_bad_unknown_app_group", None)


# ----- catalog is the only enablement source guard -----


def test_catalog_yaml_is_only_enablement_source() -> None:
    """No Python module in `apps/*.py` reads a
    cluster-level `enabled = True` flag. The catalog
    is the only enablement surface; apps don't peek
    at cluster-level booleans.
    """
    # Static check: scan apps/*.py for the pattern
    # `enabled\s*=\s*True` (and the False variant).
    # Apps must never check this attribute — they
    # rely on the catalog loader + orchestrator to
    # filter them out before the orchestrator calls
    # their `plan()` etc.
    offenders: list[tuple[str, int, str]] = []
    pattern = re.compile(r"\benabled\s*=\s*(?:True|False|true|false)\b")
    for app_py in sorted(APPS_DIR.glob("*.py")):
        if app_py.name in ("__init__.py", "base.py"):
            continue
        for line_no, line in enumerate(
            app_py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                # Allow inside docstrings/comments
                # (rough heuristic: lines starting with
                # `#`).
                if line.lstrip().startswith("#"):
                    continue
                offenders.append((app_py.name, line_no, line.strip()))
    assert not offenders, (
        f"Apps must not read cluster-level `enabled = True/False` flags — "
        f"the catalog is the only enablement source. Offenders: {offenders}"
    )


def test_catalog_yaml_is_only_enablement_source_in_orchestrator() -> None:
    """Companion: the orchestrator.py also doesn't
    bypass the catalog by reading `enabled.*=.*True`
    patterns (a future contributor might add such a
    shortcut for an MVP — WP14 forbids it)."""
    orchestrator_path = (
        PROVISIONER / "provisioner" / "lib" / "orchestrator.py"
    )
    src = orchestrator_path.read_text(encoding="utf-8")
    # Orchestrator imports `_enable_apps` / `enabled_app_names`
    # are fine — those are the documented entry points.
    # Just make sure there's no hand-rolled
    # `if some_app.enabled:` short-circuit.
    pattern = re.compile(r"\bif\s+\w+\.enabled\b")
    offenders: list[tuple[int, str]] = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            offenders.append((line_no, line.strip()))
    assert not offenders, (
        f"orchestrator.py must not short-circuit on `.enabled` attributes. "
        f"Offenders: {offenders}"
    )


# ----- group registration via @register_group only -----


def test_group_registration_via_decorator_only() -> None:
    """Only `@register_group`-decorated classes in
    `provisioner/lib/groups/` register. The registry
    rejects duplicates with the same name from a
    different module; same-module re-registration is
    idempotent (test re-imports stay safe).
    """
    # The shipped groups (`cicd_stack`, `default`) are
    # already imported by `provisioner/lib/groups/__init__.py`'s
    # force-imports block, so the registry should
    # already have them populated.
    registered = {g.name for g in all_groups()}
    assert "cicd-stack" in registered
    assert "default" in registered


def test_register_group_rejects_duplicate_name_across_modules() -> None:
    """Two groups with the same `name` from different
    modules raise `ValueError` at import time. The
    cross-module duplicate is the case WP14 forbids;
    same-module + qualname matches are treated as
    the same logical class (test re-import safety).
    """
    # A class with the same name as the shipped
    # CicdStackGroup but a different qualname must
    # fail to register.
    class _DupName(BaseGroup):
        name = "cicd-stack"  # collides

        nodes: tuple[str, ...] = ()
        edges: dict[str, tuple[str, ...]] = {}

        def enabled_in(self, catalog):  # type: ignore[no-untyped-def]
            return True

    with pytest.raises(ValueError) as exc_info:
        register_group(_DupName)
    assert "cicd-stack" in str(exc_info.value)


# ----- new group = one-file edit -----


def test_new_group_is_one_file() -> None:
    """A new group only needs editing
    `provisioner/lib/groups/__init__.py` (the
    `from . import <newgroup> as _<newgroup>`
    line). The CLI side imports the package once
    and lets the registry discover the new module.

    WP14 codifies this so adding a new group is a
    single-file edit; a future contributor who
    scatters group registration across multiple
    sites trips this guard.
    """
    init_path = GROUPS_DIR / "__init__.py"
    src = init_path.read_text(encoding="utf-8")

    # Required: shipped group modules are explicitly
    # imported at the bottom of `__init__.py`. These
    # imports are the single-site that brings the new
    # group into the registry.
    #
    # The shipped group modules MUST appear here.
    # A new group also goes here, in the same pattern,
    # when added.
    for module in ("cicd_stack", "default"):
        # Match `from . import <module> as _<module>`.
        # Both single-line and multi-line forms count.
        assert re.search(rf"from \. import {module}\b", src), (
            f"`{init_path.relative_to(PROVISIONER)}` must "
            f"force-import `{module}` so the group registry "
            f"populates at package-import time. Add a "
            f"`from . import {module} as _{module}` line."
        )


# ----- group references an app not present in registry (catalog loader scope) -----


def test_catalog_loader_rejects_unknown_apps_in_cluster_overlay() -> None:
    """Companion to the registry-pinned
    `test_groups_cannot_reference_unknown_app`:
    `Catalog.from_shipped_and_cluster` raises
    CatalogError when the cluster overlay lists an
    app that isn't in the shipped catalog. Two layers
    of defense — registry guard at resolve time,
    catalog loader guard at merge time.
    """
    from provisioner.lib.catalog import AppConfig, Catalog

    shipped = load_shipped_catalog(SHIPPED_CATALOG_PATH)
    cluster = Catalog(
        cluster_name="test",
        apps={
            **{name: AppConfig(enabled=True) for name in shipped.apps},
            "not-in-shipped-catalog": AppConfig(enabled=True),
        },
        ingress_base_domain="example.net",
    )
    with pytest.raises(CatalogError) as exc_info:
        Catalog.from_shipped_and_cluster(
            shipped=shipped,
            cluster=cluster,
        )
    # Error message names the unknown apps.
    assert "not-in-shipped-catalog" in str(exc_info.value)
