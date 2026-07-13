"""WP5 tests — bitwarden-sm-operator app."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps.bitwarden_sm import (
    CHART,
    CHART_VERSION,
    NAMESPACE,
    OPERATOR_IMAGE_VERSION,
    BitwardenSmApp,
)
from provisioner.lib.apps import app_by_name, reset_registry
from provisioner.lib.container import Container


def _make_ctx(repo: Path) -> Container:
    return Container.for_tests(
        proxmox_k3s_repo=repo,
        repo_root=repo,
        audit_log=repo / "logs" / "test.audit.jsonl",
    )


def _write_kubeconfig(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster:\n    server: https://10.0.0.64:6443\n"
        "  name: cicd\n"
        "contexts:\n- context:\n    cluster: cicd\n    user: cicd\n"
        "    namespace: default\n  name: cicd\n"
        "current-context: cicd\n"
        "users:\n- name: cicd\n  user:\n    token: t\n"
    )


def test_bitwarden_sm_is_registered_on_import() -> None:
    import importlib

    from provisioner.lib.apps import bitwarden_sm as bw_mod

    reset_registry()
    importlib.reload(bw_mod)
    assert app_by_name("bitwarden-sm-operator") is bw_mod.BitwardenSmApp


def test_bitwarden_sm_plan_pins_stable_chart_version() -> None:
    ctx = _make_ctx(Path("/tmp"))
    plan = BitwardenSmApp().plan(ctx, {})
    assert plan.app_name == "bitwarden-sm-operator"
    # Stable channel — no --devel flag in the rendered install.
    assert not any("--devel" in s for s in plan.would_install)
    # The pinned chart version surfaces in the install command.
    assert any(CHART_VERSION in s for s in plan.would_install)
    assert any("bitwardensecrets.k8s.bitwarden.com" in n for n in plan.notes)


def test_bitwarden_sm_apply_runs_helm_stable(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "values").mkdir(parents=True)
    (repo / "values" / "bitwarden-sm-operator.yaml").write_text("# ok\n")
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")

    ctx = _make_ctx(repo)

    fake_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    helm_mock.repo_add = fake_run
    helm_mock.repo_update = fake_run
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    kubectl_mock.wait_deployments_available = fake_run
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout="bitwardensecrets.k8s.bitwarden.com",
            stderr="",
        )
    )
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    result = BitwardenSmApp().apply(ctx, {})

    # helm install_or_upgrade has NO --devel extra_arg (stable channel).
    helm_calls = [
        c for c in fake_run.call_args_list if "chart" in c.kwargs
    ]
    assert len(helm_calls) == 1
    extra_args = helm_calls[0].kwargs.get("extra_args") or ()
    assert "--devel" not in extra_args
    assert helm_calls[0].kwargs["namespace"] == NAMESPACE
    assert helm_calls[0].kwargs["chart"] == CHART
    assert helm_calls[0].kwargs["version"] == CHART_VERSION

    assert result.app_name == "bitwarden-sm-operator"
    assert result.namespace == "sm-operator-system"
    assert result.image_version == OPERATOR_IMAGE_VERSION


def test_bitwarden_sm_apply_fails_when_values_missing(tmp_path: Path) -> None:
    repo = tmp_path
    ctx = _make_ctx(repo)
    with pytest.raises(FileNotFoundError) as ei:
        BitwardenSmApp().apply(ctx, {})
    assert "bitwarden-sm-operator.yaml" in str(ei.value)


def test_bitwarden_sm_status_when_release_present(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="sm-operator", stderr="")
    )
    ctx.helm = MagicMock(list_releases=fake)
    ctx.kubectl = MagicMock()
    ctx.kubectl.get = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout="bitwardensecrets.k8s.bitwarden.com",
            stderr="",
        )
    )
    s = BitwardenSmApp().status(ctx, {})
    assert s.release_present is True
    assert s.chart_version == CHART_VERSION


def test_bitwarden_sm_status_when_release_missing(tmp_path: Path) -> None:
    repo = tmp_path
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.helm = MagicMock(list_releases=fake)
    s = BitwardenSmApp().status(ctx, {})
    assert s.release_present is False
    assert any("not installed" in n for n in s.notes)


def test_bitwarden_sm_destroy_uninstalls_then_deletes_ns(tmp_path: Path) -> None:
    repo = tmp_path
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    helm_mock = MagicMock()
    helm_mock.uninstall = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    kubectl_mock.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.kubectl = kubectl_mock

    BitwardenSmApp().destroy(ctx, {})
    helm_mock.uninstall.assert_called_once()
    kubectl_mock.delete_namespace.assert_called_once_with(
        "sm-operator-system", timeout_s=120.0
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod
    from provisioner.lib.apps import bitwarden_sm as bw_mod

    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    importlib.reload(bw_mod)
    yield
    reset_registry()
