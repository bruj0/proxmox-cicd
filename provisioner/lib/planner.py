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

    def render(self) -> str:
        """Render the plan to a human-readable string for
        `cicdctl plan cicd`. The operator reads this top-to-
        bottom before deciding to apply.
        """
        lines = [f"Plan for cluster {self.cluster_name!r}:"]
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
    """
    catalog = load_catalog(catalog_path, cluster_name)
    validate_enabled_apps_exist(catalog, [a.name for a in all_apps()])

    registry = {a.name: a for a in all_apps()}
    enabled = catalog.enabled_app_names()
    catalog_dict = catalog.as_dict()

    if app_filter is not None:
        # Validate the filter names against the registry.
        # `enabled_app_names` is a subset of registry names;
        # a filter name not in the registry is a typo. A
        # filter name in the registry but not in the enabled
        # set is a disabled app — surface that as an error
        # rather than silently skipping.
        for name in app_filter:
            if name not in registry:
                raise CatalogError(
                    f"--app {name!r} is not a registered app; "
                    f"known: {sorted(registry.keys())}"
                )
            if name not in enabled:
                raise CatalogError(
                    f"--app {name!r} is registered but not "
                    f"enabled in the catalog for cluster "
                    f"{cluster_name!r}"
                )
        enabled = app_filter

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
