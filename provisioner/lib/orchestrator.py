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
from typing import Any

from .apps import AppApplyResult, all_apps
from .catalog import Catalog, CatalogError, load_catalog, validate_enabled_apps_exist
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

    @staticmethod
    def _resolve_apply_order(
        catalog: Catalog,
        app_filter: list[str] | None,
        log: Any,
    ) -> list[str]:
        """Decide which apps to iterate, in what order.

        `None` (the default, no `--app` flag) means
        "every enabled app, sorted alphabetically by
        catalog order" (which is what the catalog
        returns).

        A non-empty `app_filter` is treated as the
        authoritative order (operator-typed order, not
        alphabetical). We still validate that every
        filter entry is enabled in the catalog — a
        typo or a disabled app surfaces here as a
        CatalogError instead of silently no-op'ing.
        """
        if not app_filter:
            return catalog.enabled_app_names()
        enabled = set(catalog.enabled_app_names())
        for name in app_filter:
            if name not in enabled:
                raise CatalogError(
                    f"--app {name!r} is registered but not "
                    f"enabled in the catalog (enabled: "
                    f"{sorted(enabled)})"
                )
        log.info(
            "apply.app_filter_resolved",
            requested=app_filter,
            applied=app_filter,
        )
        return app_filter

    # ------------------------------------------------------ validate

    def validate(self, cluster: str) -> int:
        """Parse catalog + check values files exist. No kubectl/helm."""
        log = self.container.logger
        log.info("validate.started", cluster=cluster)
        try:
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            log.error(
                "validate.catalog_failed",
                error=str(exc),
                resolution="fix infra/clusters/<name>/catalog.yaml",
            )
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
        log.info(
            "validate.finished",
            cluster=cluster,
            apps=catalog.enabled_app_names(),
            result="ok",
        )
        print(f"validate ok: cluster={cluster} apps={catalog.enabled_app_names()}")
        return EXIT_OK

    # ------------------------------------------------------ plan

    def plan(self, cluster: str, app_filter: list[str] | None = None) -> int:
        log = self.container.logger
        log.info(
            "plan.started",
            cluster=cluster,
            app_filter=app_filter,
        )
        try:
            self._set_cluster_env(cluster)
            plan_diff = build_plan(
                self.container,
                cluster,
                self._catalog_path(cluster),
                app_filter=app_filter,
            )
        except CatalogError as exc:
            log.error(
                "plan.catalog_failed",
                error=str(exc),
                resolution="fix infra/clusters/<name>/catalog.yaml",
            )
            print(f"plan failed: {exc}")
            return EXIT_CATALOG
        log.info(
            "plan.finished",
            cluster=cluster,
            errors=plan_diff.errors,
            apps=len(plan_diff.rows),
            skipped=plan_diff.skipped,
        )
        print(plan_diff.render())
        return EXIT_OK if not plan_diff.errors else EXIT_PLAN

    # ------------------------------------------------------ apply

    def apply(
        self,
        cluster: str,
        app_filter: list[str] | None = None,
    ) -> int:
        log = self.container.logger
        log.info(
            "apply.started",
            cluster=cluster,
            app_filter=app_filter,
        )
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
            apply_order = self._resolve_apply_order(
                catalog, app_filter, log
            )
        except CatalogError as exc:
            log.error(
                "apply.catalog_failed",
                error=str(exc),
                resolution="fix infra/clusters/<name>/catalog.yaml",
            )
            print(f"apply failed: {exc}")
            return EXIT_CATALOG

        log.info(
            "apply.catalog_loaded",
            cluster=cluster,
            apps=apply_order,
        )

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

        for name in apply_order:
            app_cls = registry.get(name)
            if app_cls is None:
                log.warn(
                    "apply.app_skipped",
                    app=name,
                    resolution="register the app via @register decorator",
                )
                print(f"  ! {name}: not in registry, skipping")
                continue
            log.info("apply.app_started", app=name)
            print(f"  -> applying {name}...")
            try:
                result = app_cls().apply(self.container, catalog_dict)
                applied.append(result)
                log.info(
                    "apply.app_completed",
                    app=name,
                    namespace=result.namespace,
                    release=result.release,
                    chart_version=result.chart_version,
                    image_version=result.image_version,
                    ingress_host=result.ingress_host,
                    next_step=result.next_step,
                )
                print(
                    f"     ok: namespace={result.namespace} "
                    f"release={result.release} "
                    f"chart={result.chart_version} "
                    f"image={result.image_version}"
                )
                # If the app returned an ingress_host, surface
                # the URL right after the install line — the
                # operator's terminal is the primary UI, not
                # the audit log.
                if result.ingress_host:
                    print(
                        f"     ingress: https://{result.ingress_host}"
                    )
                # If the app returned a post-apply `next_step`,
                # surface it here so the operator sees the
                # manual follow-up without having to tail the
                # audit log. The `apply()` method is the
                # canonical place to set this (apps own their
                # own "what's next" story; the orchestrator
                # just prints it).
                if result.next_step:
                    print(f"     next: {result.next_step}")
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "apply.app_failed",
                    app=name,
                    error=repr(exc),
                    resolution="see the audit log for the failing step",
                )
                print(f"     FAILED: {exc!r}")
                # Write the partial apps.json so the operator
                # can see what succeeded.
                self._write_apps_json(cluster, applied)
                return EXIT_APPLY

        # Write apps.json handoff for downstream consumers.
        self._write_apps_json(cluster, applied)
        log.info(
            "apply.finished",
            cluster=cluster,
            apps_installed=[a.app_name for a in applied],
            count=len(applied),
        )
        print(f"apply complete: {len(applied)} apps installed")
        return EXIT_OK

    # ------------------------------------------------------ destroy

    def destroy(
        self,
        cluster: str,
        app_filter: list[str] | None = None,
    ) -> int:
        log = self.container.logger
        log.info(
            "destroy.started",
            cluster=cluster,
            app_filter=app_filter,
        )
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
            destroy_order = self._resolve_apply_order(
                catalog, app_filter, log
            )
        except CatalogError as exc:
            log.error(
                "destroy.catalog_failed",
                error=str(exc),
                resolution="fix infra/clusters/<name>/catalog.yaml",
            )
            print(f"destroy failed: {exc}")
            return EXIT_CATALOG

        # Destroy in reverse registration order so dependents
        # (gitea-runner) are removed before their dependencies
        # (gitea). When `--app` narrows the set, reverse that
        # subset instead so the relative ordering within the
        # filter is preserved.
        ordered = list(reversed(destroy_order))
        registry = {a.name: a for a in all_apps()}
        for name in ordered:
            app_cls = registry.get(name)
            if app_cls is None:
                continue
            log.info("destroy.app_started", app=name)
            print(f"  -> destroying {name}...")
            try:
                app_cls().destroy(self.container, catalog.as_dict())
                log.info("destroy.app_completed", app=name)
                print("     ok")
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "destroy.app_failed",
                    app=name,
                    error=repr(exc),
                )
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
        log.info("destroy.finished", cluster=cluster)
        return EXIT_OK

    # ------------------------------------------------------ status

    def status(self, cluster: str) -> int:
        log = self.container.logger
        log.info("status.started", cluster=cluster)
        try:
            self._set_cluster_env(cluster)
            catalog = load_catalog(self._catalog_path(cluster), cluster)
            validate_enabled_apps_exist(
                catalog, [a.name for a in all_apps()]
            )
        except CatalogError as exc:
            log.error(
                "status.catalog_failed",
                error=str(exc),
                resolution="fix infra/clusters/<name>/catalog.yaml",
            )
            print(f"status failed: {exc}")
            return EXIT_CATALOG

        registry = {a.name: a for a in all_apps()}
        rows: list[tuple[str, str, str, str, str]] = []
        for name in catalog.enabled_app_names():
            app_cls = registry.get(name)
            if app_cls is None:
                continue
            log.info("status.app_probed", app=name)
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
                log.info(
                    "status.app_completed",
                    app=name,
                    release_present=s.release_present,
                    chart_version=s.chart_version,
                    image_version=s.image_version,
                    notes=s.notes,
                )
                for n in s.notes:
                    rows.append(("", "  note: " + n, "", "", ""))
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "status.app_failed",
                    app=name,
                    error=repr(exc),
                )
                rows.append((name, "?", "error", "-", str(exc)[:40]))

        # Pretty-print as a table.
        if not rows:
            log.info("status.finished", cluster=cluster, apps=0)
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
        log.info(
            "status.finished",
            cluster=cluster,
            apps_probed=len(rows),
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
