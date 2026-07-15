"""planner — diff desired (catalog) vs live (kubectl/helm) state.

Mirrors proxmox-k3s/provisioner/lib/planner.py in spirit but
is much smaller: the apps catalog has no infrastructure-level
state (no VMs, no kube cluster), only helm releases + CRDs.
Each AppSpec implements `plan()` itself; the orchestrator's
planner just collects and prints them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .apps import AppPlanResult, all_apps
from .catalog import CatalogError, load_catalog, validate_enabled_apps_exist
from .container import Container


@dataclass
class PlanDiff:
    """The full plan output: one row per enabled app."""

    cluster_name: str
    rows: list[AppPlanResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self, group: str = "default") -> str:
        """Render the plan to a human-readable string for
        `cicdctl plan cicd`. The operator reads this top-to-
        bottom before deciding to apply.

        WP3 — `group` is the resolved group name
        (from `--group` on the CLI, or `default` if
        unset). Rendered as a single line under the
        header so the operator knows which DAG ran.
        """
        lines = [
            f"Plan for cluster {self.cluster_name!r}:",
            f"  Group: {group}",
        ]
        if self.errors:
            lines.append("")
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
            return "\n".join(lines) + "\n"
        if not self.rows and not self.skipped:
            lines.append("  (no apps selected)")
            return "\n".join(lines) + "\n"
        if self.skipped:
            lines.append("")
            lines.append(
                "  skipped (not enabled in catalog): "
                + ", ".join(self.skipped)
            )
        lines.append("")
        for r in self.rows:
            lines.append(f"  + app: {r.app_name}")
            for s in r.would_install:
                lines.append(f"      install: {s}")
            for s in r.would_apply:
                lines.append(f"      apply:   {s}")
            for n in r.notes:
                lines.append(f"      note:    {n}")
        return "\n".join(lines) + "\n"


def build_plan(
    container: Container,
    cluster_name: str,
    catalog_path: Path,
    app_filter: list[str] | None = None,
    group: str = "default",
) -> PlanDiff:
    """Build a PlanDiff from the catalog + registered apps.

    Calls each enabled app's `.plan(ctx, catalog.as_dict())`
    and aggregates. Apps not in the catalog but registered
    appear in `skipped`. Apps in the catalog but unknown
    raise CatalogError (caught by the orchestrator and
    surfaced in `errors`).

    `app_filter` is an order-preserving list of app names
    to plan; `None` means "every enabled app". This is the
    `--app` flag from `cicdctl plan --app <name>`. When
    provided, the filter is checked against the registry
    first so a misspelled name surfaces as a catalog error
    instead of a silent empty plan.

    WP3 — `group` is the resolved group name. The
    planner iterates apps in the group's topological
    order (intersected with `app_filter` and the
    catalog's enabled set). Unknown group names
    surface as `CatalogError` via
    `resolve_apply_order`.
    """
    catalog = load_catalog(catalog_path, cluster_name)
    validate_enabled_apps_exist(catalog, [a.name for a in all_apps()])

    registry = {a.name: a for a in all_apps()}
    catalog_dict = catalog.as_dict()

    # WP3: resolve which apps to plan via the groups
    # resolver. The default group is the sentinel
    # "every enabled app in catalog order". An
    # `app_filter` narrows the result. Unknown group
    # names raise `CatalogError` (`resolve_apply_order`
    # in groups/__init__.py).
    from .groups import resolve_apply_order

    enabled = resolve_apply_order(
        catalog, group, app_filter
    )

    # Filter validation: a filter name not in the
    # registry is a typo; a filter name in the
    # registry but not in the group's resolved order
    # is a disabled app. The group resolver already
    # raises CatalogError for unknown groups, but we
    # also want the registry-typo path to surface.
    if app_filter is not None:
        for name in app_filter:
            if name not in registry:
                raise CatalogError(
                    f"--app {name!r} is not a registered "
                    f"app; known: {sorted(registry.keys())}"
                )

    plan = PlanDiff(cluster_name=cluster_name)
    for name in enabled:
        app_cls = registry.get(name)
        if app_cls is None:
            plan.errors.append(
                f"enabled app {name!r} not in registry (this "
                f"should have been caught by validate_enabled_apps_exist)"
            )
            continue
        try:
            row: AppPlanResult = app_cls().plan(container, catalog_dict)
        except Exception as exc:  # noqa: BLE001 — surface to operator
            plan.errors.append(f"{name}: {exc!r}")
            continue
        plan.rows.append(row)

    plan.skipped = sorted(
        name for name in registry if name not in enabled
    )
    return plan


__all__ = ["PlanDiff", "build_plan"]
