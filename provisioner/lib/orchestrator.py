"""orchestrator — top-level entry point for plan/apply/destroy/status/validate.

Mirrors proxmox-k3s/provisioner/lib/orchestrator.py in shape
(an `Orchestrator` class with one method per CLI subcommand)
but the workload is much smaller: there's no bootstrap
state JSON, no PVE API calls, no SSH. Just catalog -> apps.

App-specific imports live in `apps/__init__.py` via the
`@register` decorator; the orchestrator only knows the
`AppSpec` protocol.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .apps import AppApplyResult, all_apps
from .catalog import CatalogError, load_catalog, validate_enabled_apps_exist
from .container import Container
from .output_writer import write_apps_json
from .planner import build_plan

EXIT_OK = 0
EXIT_CATALOG = 3
EXIT_PLAN = 4
EXIT_APPLY = 5
EXIT_DESTROY = 6
EXIT_STATUS = 7
EXIT_VALIDATE = 8


@dataclass
class Orchestrator:
    """Coordinates plan/apply/destroy/status across the catalog."""

    container: Container

    def _catalog_path(self, cluster: str) -> Path:
        return self.container.repo_root / "infra" / "clusters" / cluster / "catalog.yaml"

    def _kubeconfig_path(self, cluster: str) -> Path:
        return (
            self.container.proxmox_k3s_repo
            / "infra"
            / "clusters"
            / cluster
            / "kubeconfig.yaml"
        )

    def _set_cluster_env(self, cluster: str) -> None:
        """Apps read this env var to find the kubeconfig path."""
        os.environ["PROXMOX_CICD_CLUSTER"] = cluster

    # ------------------------------------------------------ validate

    def validate(self, cluster: str) -> int:
        """Parse catalog + check values files exist. No kubectl/helm."""
        try:
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            print(f"validate failed: {exc}")
            return EXIT_VALIDATE
        # Probe values files for each enabled app.
        for name in catalog.enabled_app_names():
            app_cls = next((a for a in all_apps() if a.name == name), None)
            if app_cls is None:
                continue
            # Apps self-report their values file path. We just
            # check that `values/<app>.yaml` exists for the
            # common case. Apps with no values file (e.g. the
            # gitea-runner chart with all chart-default values)
            # are fine.
            candidates = [
                self.container.repo_root / "values" / f"{name}.yaml",
                self.container.repo_root / "values" / f"{name.replace('-', '_')}.yaml",
            ]
            if not any(p.exists() for p in candidates):
                # Apps are allowed to ship no values file
                # (chart defaults). The orchestrator doesn't
                # warn; the app's apply() will tell you if
                # one is required.
                pass
        print(f"validate ok: cluster={cluster} apps={catalog.enabled_app_names()}")
        return EXIT_OK

    # ------------------------------------------------------ plan

    def plan(self, cluster: str) -> int:
        try:
            self._set_cluster_env(cluster)
            plan_diff = build_plan(
                self.container,
                cluster,
                self._catalog_path(cluster),
            )
        except CatalogError as exc:
            print(f"plan failed: {exc}")
            return EXIT_CATALOG
        print(plan_diff.render())
        return EXIT_OK if not plan_diff.errors else EXIT_PLAN

    # ------------------------------------------------------ apply

    def apply(self, cluster: str) -> int:
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            print(f"apply failed: {exc}")
            return EXIT_CATALOG

        # Pre-flight: kubectl + helm on PATH, kubeconfig exists.
        from .kubectl_runner import helm_on_path, kubectl_on_path

        if not kubectl_on_path():
            print("kubectl not on PATH")
            return EXIT_APPLY
        if not helm_on_path():
            print("helm not on PATH")
            return EXIT_APPLY
        if not self._kubeconfig_path(cluster).exists():
            print(
                f"kubeconfig.yaml not found at "
                f"{self._kubeconfig_path(cluster)}. "
                f"Did you run `make apply` in proxmox-k3s?"
            )
            return EXIT_APPLY

        registry = {a.name: a for a in all_apps()}
        catalog_dict = catalog.as_dict()
        applied: list[AppApplyResult] = []

        for name in catalog.enabled_app_names():
            app_cls = registry.get(name)
            if app_cls is None:
                print(f"  ! {name}: not in registry, skipping")
                continue
            print(f"  -> applying {name}...")
            try:
                result = app_cls().apply(self.container, catalog_dict)
                applied.append(result)
                print(
                    f"     ok: namespace={result.namespace} "
                    f"release={result.release} "
                    f"chart={result.chart_version} "
                    f"image={result.image_version}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"     FAILED: {exc!r}")
                # Write the partial apps.json so the operator
                # can see what succeeded.
                self._write_apps_json(cluster, applied)
                return EXIT_APPLY

        # Write apps.json handoff for downstream consumers.
        self._write_apps_json(cluster, applied)
        print(f"apply complete: {len(applied)} apps installed")
        return EXIT_OK

    # ------------------------------------------------------ destroy

    def destroy(self, cluster: str) -> int:
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            print(f"destroy failed: {exc}")
            return EXIT_CATALOG

        # Destroy in reverse registration order so dependents
        # (gitea-runner) are removed before their dependencies
        # (gitea).
        ordered = list(reversed(catalog.enabled_app_names()))
        registry = {a.name: a for a in all_apps()}
        for name in ordered:
            app_cls = registry.get(name)
            if app_cls is None:
                continue
            print(f"  -> destroying {name}...")
            try:
                app_cls().destroy(self.container, catalog.as_dict())
                print("     ok")
            except Exception as exc:  # noqa: BLE001
                print(f"     FAILED: {exc!r}")
                # Continue destroying the rest.
        # Remove the handoff.
        apps_json = (
            self.container.repo_root
            / "infra"
            / "clusters"
            / cluster
            / "apps.json"
        )
        if apps_json.exists():
            apps_json.unlink()
        return EXIT_OK

    # ------------------------------------------------------ status

    def status(self, cluster: str) -> int:
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            print(f"status failed: {exc}")
            return EXIT_CATALOG

        registry = {a.name: a for a in all_apps()}
        rows: list[tuple[str, str, str, str, str]] = []
        for name in catalog.enabled_app_names():
            app_cls = registry.get(name)
            if app_cls is None:
                continue
            try:
                s = app_cls().status(self.container, catalog.as_dict())
                rows.append(
                    (
                        s.app_name,
                        s.namespace,
                        "yes" if s.release_present else "no",
                        s.chart_version or "-",
                        s.image_version or "-",
                    )
                )
                for n in s.notes:
                    rows.append(("", "  note: " + n, "", "", ""))
            except Exception as exc:  # noqa: BLE001
                rows.append((name, "?", "error", "-", str(exc)[:40]))

        # Pretty-print as a table.
        if not rows:
            print("no apps to report")
            return EXIT_OK
        widths = [20, 24, 9, 10, 14]
        header = ("app", "namespace", "installed", "chart", "image")
        print(
            f"{header[0]:<{widths[0]}} {header[1]:<{widths[1]}} "
            f"{header[2]:<{widths[2]}} {header[3]:<{widths[3]}} "
            f"{header[4]:<{widths[4]}}"
        )
        print("-" * sum(widths) + "-" * 4)
        for r in rows:
            print(
                f"{r[0]:<{widths[0]}} {r[1]:<{widths[1]}} "
                f"{r[2]:<{widths[2]}} {r[3]:<{widths[3]}} "
                f"{r[4]:<{widths[4]}}"
            )
        return EXIT_OK

    # ------------------------------------------------------ helpers

    def _write_apps_json(
        self, cluster: str, applied: list[AppApplyResult]
    ) -> None:
        path = write_apps_json(self.container.repo_root, cluster, applied)
        self.container.logger.info(
            "apps_json_written", path=str(path), apps=len(applied)
        )


__all__ = ["Orchestrator", "EXIT_OK", "EXIT_CATALOG", "EXIT_PLAN", "EXIT_APPLY",
           "EXIT_DESTROY", "EXIT_STATUS", "EXIT_VALIDATE"]


# Touch unused imports so ruff + mypy are happy when only
# some are referenced via the orchestrator's plumbing.
_ = sys
