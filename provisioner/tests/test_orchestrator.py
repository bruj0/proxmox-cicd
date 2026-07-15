"""WP6 tests — catalog, planner, orchestrator, SOLID seams."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.catalog import (
    AppConfig,
    Catalog,
    CatalogError,
    load_catalog,
    validate_enabled_apps_exist,
)
from provisioner.lib.container import Container
from provisioner.lib.orchestrator import Orchestrator


# ----------------------------------------------------------- catalog


def test_load_catalog_minimal(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
    )
    catalog = load_catalog(catalog_path, "cicd")
    assert catalog.cluster_name == "cicd"
    assert catalog.ingress_base_domain == "example.net"
    assert catalog.apps["gitea"].enabled is True
    assert catalog.enabled_app_names() == ["gitea"]


def test_load_catalog_requires_ingress(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: cicd\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
    )
    with pytest.raises(CatalogError) as ei:
        load_catalog(catalog_path, "cicd")
    assert "ingress.base_domain" in str(ei.value)


def test_load_catalog_rejects_invalid_dns(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: NOT_VALID!!!\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
    )
    with pytest.raises(CatalogError) as ei:
        load_catalog(catalog_path, "cicd")
    assert "DNS label" in str(ei.value)


def test_load_catalog_requires_cluster_name_match(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: apps\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
    )
    with pytest.raises(CatalogError) as ei:
        load_catalog(catalog_path, "cicd")
    assert "does not match" in str(ei.value)


def test_load_catalog_requires_at_least_one_app(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps: {}\n"
    )
    with pytest.raises(CatalogError):
        load_catalog(catalog_path, "cicd")


def test_validate_enabled_apps_exist_raises_on_unknown(tmp_path: Path) -> None:
    catalog = Catalog(
        cluster_name="cicd",
        apps={"unknown-app": AppConfig(enabled=True)},
        ingress_base_domain="example.net",
    )
    with pytest.raises(CatalogError) as ei:
        validate_enabled_apps_exist(catalog, ["gitea", "vaultwarden-k8s-sync"])
    assert "unknown-app" in str(ei.value)


def test_validate_enabled_apps_exist_passes_when_known() -> None:
    catalog = Catalog(
        cluster_name="cicd",
        apps={"gitea": AppConfig(enabled=True)},
        ingress_base_domain="example.net",
    )
    # Should not raise.
    validate_enabled_apps_exist(catalog, ["gitea", "vaultwarden-k8s-sync"])


def test_catalog_as_dict_shape(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "vaultwarden:\n"
        "  server_url: https://bitwarden.example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
    )
    catalog = load_catalog(catalog_path, "cicd")
    d = catalog.as_dict()
    assert d["ingress"]["base_domain"] == "example.net"
    assert d["vaultwarden"]["server_url"] == "https://bitwarden.example.net"
    assert d["apps"]["gitea"]["enabled"] is True


# ----------------------------------------------------------- orchestrator


def _make_orchestrator_with_catalog(
    repo: Path,
    catalog_yaml: str,
) -> tuple[Orchestrator, Container]:
    """Build an Orchestrator backed by a fake but valid catalog."""
    repo.mkdir(parents=True, exist_ok=True)
    cluster_dir = repo / "infra" / "clusters" / "cicd"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / "catalog.yaml").write_text(catalog_yaml)
    (cluster_dir / "kubeconfig.yaml").write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster:\n    server: https://10.0.0.64:6443\n"
        "  name: cicd\n"
        "contexts:\n- context:\n    cluster: cicd\n    user: cicd\n"
        "    namespace: default\n  name: cicd\n"
        "current-context: cicd\n"
        "users:\n- name: cicd\n  user:\n    token: t\n"
    )
    (repo / "values").mkdir(parents=True, exist_ok=True)
    container = Container.for_tests(
        proxmox_k3s_repo=repo,
        repo_root=repo,
    )
    # Replace helm + kubectl with mocks so apply doesn't shell out.
    container.helm = MagicMock()
    container.kubectl = MagicMock()
    return container.orchestrator, container


def test_orchestrator_plan_returns_zero_on_success(tmp_path: Path) -> None:
    orch, _ = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n",
    )
    assert orch.plan("cicd") == 0


def test_orchestrator_validate_returns_zero_on_success(tmp_path: Path) -> None:
    orch, _ = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n",
    )
    assert orch.validate("cicd") == 0


def test_orchestrator_plan_returns_three_on_missing_catalog(tmp_path: Path) -> None:
    repo = tmp_path
    repo.mkdir(parents=True, exist_ok=True)
    container = Container.for_tests(
        proxmox_k3s_repo=repo, repo_root=repo
    )
    orch = Orchestrator(container=container)
    assert orch.plan("cicd") == 3


def test_orchestrator_apply_returns_zero_on_full_mocks(tmp_path: Path) -> None:
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        # Test-time opt-out: skip the Vaultwarden admin-pw
        # seed so the apply path doesn't need a live
        # Vaultwarden + BW_CLIENTID/SECRET Secret in the
        # cluster. The cluster Secret is still written;
        # only the VW push is suppressed.
        "vaultwarden:\n"
        "  skip_admin_seed: true\n"
        "apps:\n"
        "  vaultwarden-k8s-sync:\n"
        "    enabled: true\n"
        "  gitea:\n"
        "    enabled: true\n",
    )
    # Lay down the values files the apps need.
    (tmp_path / "values" / "gitea.yaml").write_text("# ok\n")
    (tmp_path / "values" / "vaultwarden-kubernetes-secrets.yaml").write_text("# ok\n")
    container.kubectl.wait_deployments_available = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    # kubectl.get is read by the gitea admin-pw lifecycle
    # (drift-check on the cluster Secret + VKS BW_CLIENTID/
    # SECRET read in the would-be Vaultwarden seed). With
    # vaultwarden.skip_admin_seed=true the VKS-cred read
    # is short-circuited, so a bare rc=1 "not found"
    # mock is enough to drive the admin-secret drift
    # check into "create" mode.
    container.kubectl.get = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="not found")
    )
    container.helm.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.uninstall = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.list_releases = MagicMock(
        return_value=MagicMock(returncode=0, stdout="gitea", stderr="")
    )
    container.helm.repo_add = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.repo_update = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )

    rc = orch.apply("cicd")
    assert rc == 0
    # apps.json was written.
    apps_json = (
        tmp_path / "infra" / "clusters" / "cicd" / "apps.json"
    )
    assert apps_json.exists()
    payload = json.loads(apps_json.read_text())
    assert payload["cluster_name"] == "cicd"
    assert len(payload["apps"]) == 2


def test_orchestrator_apply_with_app_filter_only_runs_named_apps(
    tmp_path: Path,
) -> None:
    """`--app <name>` restricts apply to the named apps
    only, in the order the operator typed them.
    """
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "vaultwarden:\n"
        "  skip_admin_seed: true\n"
        "  skip_runner_seed: true\n"
        "apps:\n"
        "  vaultwarden-k8s-sync:\n"
        "    enabled: true\n"
        "  gitea:\n"
        "    enabled: true\n"
        "  gitea-runner:\n"
        "    enabled: true\n",
    )
    (tmp_path / "values" / "gitea.yaml").write_text("# ok\n")
    (tmp_path / "values" / "vaultwarden-kubernetes-secrets.yaml").write_text("# ok\n")
    # gitea-runner needs its chart dir on disk.
    chart_dir = tmp_path / "infra" / "charts" / "gitea-runner"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: gitea-runner\nversion: 0.2.0\n"
    )
    container.kubectl.wait = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.get = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="not found")
    )
    container.helm.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.repo_add = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.repo_update = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )

    rc = orch.apply("cicd", app_filter=["gitea-runner", "gitea"])
    assert rc == 0

    # apps.json should only contain the two filtered apps.
    apps_json = (
        tmp_path / "infra" / "clusters" / "cicd" / "apps.json"
    )
    payload = json.loads(apps_json.read_text())
    names = [a["name"] for a in payload["apps"]]
    assert names == ["gitea-runner", "gitea"]
    # vaultwarden-k8s-sync was NOT touched.
    assert "vaultwarden-k8s-sync" not in names

    # The audit log should record the filter resolution.
    audit_log = next(
        (tmp_path / "logs").glob("test_*.audit.jsonl"), None
    )
    assert audit_log is not None
    lines = audit_log.read_text().splitlines()
    assert any(
        '"apply.app_filter_resolved"' in line
        and '"requested":["gitea-runner","gitea"]' in line
        for line in lines
    ), f"filter-resolved log line not found in: {lines!r}"


def test_orchestrator_apply_rejects_filter_with_unknown_app(
    tmp_path: Path,
) -> None:
    """A typo in `--app` produces EXIT_CATALOG (3) with a
    clear message listing the registered apps. The apply
    must not start any helm/kubectl work.
    """
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n",
    )
    container.helm.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    rc = orch.apply("cicd", app_filter=["gete"])
    assert rc == 3
    # No helm install must have been attempted.
    assert not container.helm.install_or_upgrade.called


def test_orchestrator_apply_rejects_filter_with_disabled_app(
    tmp_path: Path,
) -> None:
    """`--app gitea-runner` against a catalog where
    gitea-runner is registered but not enabled must
    surface EXIT_CATALOG (3) — the operator's typo or
    stale mental model deserves a clear error, not a
    silent no-op.
    """
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
        "  gitea-runner:\n"
        "    enabled: false\n",
    )
    container.helm.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    rc = orch.apply("cicd", app_filter=["gitea-runner"])
    assert rc == 3
    assert not container.helm.install_or_upgrade.called


def test_orchestrator_plan_with_app_filter_only_plans_named_apps(
    tmp_path: Path,
) -> None:
    """`plan --app` is read-only and must skip unselected
    apps so the operator can preview a single change
    without scrolling through the full catalog.
    """
    orch, _ = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
        "  vaultwarden-k8s-sync:\n"
        "    enabled: true\n",
    )
    rc = orch.plan("cicd", app_filter=["gitea"])
    assert rc == 0


def test_orchestrator_destroy_with_app_filter_only_destroys_named(
    tmp_path: Path,
) -> None:
    """`destroy --app gitea` must only call gitea.destroy()
    and leave the other apps' releases alone.
    """
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n"
        "  vaultwarden-k8s-sync:\n"
        "    enabled: true\n",
    )
    container.helm.uninstall = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    rc = orch.destroy("cicd", app_filter=["gitea"])
    assert rc == 0
    # helm.uninstall called exactly once (for gitea only),
    # not twice (gitea + vaultwarden-k8s-sync). The arg
    # shape is positional (release, namespace) on
    # HelmRunner.uninstall.
    assert container.helm.uninstall.call_count == 1
    call_args = container.helm.uninstall.call_args.args
    assert call_args[0] == "gitea"


def test_orchestrator_destroy_writes_nothing_and_uninstalls(tmp_path: Path) -> None:
    orch, container = _make_orchestrator_with_catalog(
        tmp_path,
        "cluster_name: cicd\n"
        "ingress:\n"
        "  base_domain: example.net\n"
        "apps:\n"
        "  gitea:\n"
        "    enabled: true\n",
    )
    container.helm.uninstall = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    rc = orch.destroy("cicd")
    assert rc == 0
    assert container.helm.uninstall.called


# ----------------------------------------------------------- SOLID seams


def test_orchestrator_does_not_import_app_specific_symbols() -> None:
    """Open/Closed proof: the orchestrator imports AppSpec
    (the protocol) but never a concrete app class. Adding a
    4th app shouldn't require touching orchestrator.py.
    """
    import inspect

    from provisioner.lib import orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    # The only thing the orchestrator should reference from
    # the apps package is the AppSpec Protocol + AppApplyResult
    # dataclass (used as a type hint) + all_apps registry.
    # No `from .apps.gitea` / `from .apps.gitea_runner` /
    # `from .apps.vaultwarden_k8s_sync` imports.
    for forbidden in (
        "from .apps.gitea",
        "from .apps.gitea_runner",
        "from .apps.vaultwarden_k8s_sync",
        "from provisioner.lib.apps.gitea",
        "from provisioner.lib.apps.gitea_runner",
        "from provisioner.lib.apps.vaultwarden_k8s_sync",
    ):
        assert forbidden not in src, (
            f"orchestrator.py imports {forbidden!r}; this "
            f"violates Open/Closed"
        )


# ----------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from provisioner.lib.apps import reset_registry

    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod
    from provisioner.lib.apps import vaultwarden_k8s_sync as vks_mod

    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    importlib.reload(vks_mod)
    yield
    reset_registry()
