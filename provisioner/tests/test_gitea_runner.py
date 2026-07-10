"""WP4 tests — gitea-runner app + owned chart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps.gitea_runner import (
    APP_VERSION,
    BW_SECRET_CR,
    CHART_VERSION,
    GiteaRunnerApp,
    NAMESPACE,
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


def test_gitea_runner_app_is_registered_on_import() -> None:
    import importlib

    from provisioner.lib.apps import gitea_runner as gr_mod

    reset_registry()
    importlib.reload(gr_mod)
    assert app_by_name("gitea-runner") is gr_mod.GiteaRunnerApp


def test_gitea_runner_plan_mentions_local_chart_and_bw(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    plan = GiteaRunnerApp().plan(ctx, {})
    assert plan.app_name == "gitea-runner"
    assert any("helm upgrade --install gitea-runner" in s for s in plan.would_install)
    assert any("BitwardenSecret" in s for s in plan.would_apply)
    assert any("ephemeral: true" in n for n in plan.notes)


def test_gitea_runner_apply_uses_local_chart_path(tmp_path: Path) -> None:
    repo = tmp_path
    chart_dir = repo / "infra" / "charts" / "gitea-runner"
    chart_dir.mkdir(parents=True)
    # Lay down a minimal Chart.yaml so helm would accept it.
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: gitea-runner\nversion: 0.1.0\n"
    )
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")

    ctx = _make_ctx(repo)

    fake_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    # The CRD check needs a non-empty stdout to pass.
    crd_present = MagicMock(
        return_value=MagicMock(
            returncode=0, stdout="bitwardensecrets.k8s.bitwarden.com", stderr=""
        )
    )

    def fake_apply(*args: object, **kwargs: object) -> MagicMock:
        return MagicMock(returncode=0, stdout="", stderr="")

    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    kubectl_mock.apply = MagicMock(side_effect=fake_apply)
    kubectl_mock.get = crd_present
    kubectl_mock.wait_deployments_available = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    result = GiteaRunnerApp().apply(
        ctx,
        {
            "bitwarden": {
                "organization_id": "00000000-0000-0000-0000-000000000001",
                "runner_secret_id": "00000000-0000-0000-0000-000000000002",
            }
        },
    )

    # helm was called with the local chart path, not an OCI URL.
    # fake_run serves both helm.install_or_upgrade and the wait/delete
    # kubectl paths; the helm call has `chart=` kwarg.
    helm_calls = [
        c for c in fake_run.call_args_list if "chart" in c.kwargs
    ]
    assert len(helm_calls) >= 1, f"expected helm call; got {len(helm_calls)}"
    args, kwargs = helm_calls[0]
    assert str(kwargs["chart"]).endswith("/infra/charts/gitea-runner")
    assert kwargs["namespace"] == NAMESPACE
    assert kwargs["release"] == "gitea-runner"

    # BitwardenSecret CR was applied.
    cr_call = kubectl_mock.apply.call_args_list[0]
    # The orchestrator calls `kubectl.apply(manifest=..., namespace=...)`
    # so the manifest is in `kwargs['manifest']`, not `args[0]`.
    cr_kwargs = cr_call.kwargs
    if "manifest" in cr_kwargs:
        cr_input = cr_kwargs["manifest"]
    elif "input" in cr_kwargs:
        cr_input = cr_kwargs["input"]
    elif cr_call.args:
        cr_input = cr_call.args[0]
    else:
        pytest.fail(f"no manifest kwarg in apply call: {cr_call!r}")
    assert BW_SECRET_CR in cr_input
    assert "bitwarden" in cr_input.lower()

    assert result.app_name == "gitea-runner"
    assert result.namespace == "gitea-runner"


def test_gitea_runner_apply_fails_when_bitwarden_crd_missing(tmp_path: Path) -> None:
    repo = tmp_path
    chart_dir = repo / "infra" / "charts" / "gitea-runner"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: gitea-runner\nversion: 0.1.0\n"
    )
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")

    ctx = _make_ctx(repo)
    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = MagicMock()
    helm_mock.uninstall = MagicMock()
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    # CRD check returns empty -> no CRD registered.
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    kubectl_mock.apply = MagicMock()
    kubectl_mock.wait_deployments_available = MagicMock()
    kubectl_mock.delete_namespace = MagicMock()
    ctx.kubectl = kubectl_mock

    with pytest.raises(RuntimeError) as ei:
        GiteaRunnerApp().apply(ctx, {})
    assert "CRD is not installed" in str(ei.value)


def test_gitea_runner_status_when_release_present(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="gitea-runner", stderr="")
    )
    ctx.helm = MagicMock(list_releases=fake)

    s = GiteaRunnerApp().status(ctx, {})
    assert s.app_name == "gitea-runner"
    assert s.namespace == "gitea-runner"
    assert s.release_present is True
    assert s.image_version == APP_VERSION


def test_gitea_runner_destroy_uninstalls_then_deletes_ns(tmp_path: Path) -> None:
    repo = tmp_path
    chart_dir = repo / "infra" / "charts" / "gitea-runner"
    chart_dir.mkdir(parents=True)
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

    GiteaRunnerApp().destroy(ctx, {})
    helm_mock.uninstall.assert_called_once()
    kubectl_mock.delete_namespace.assert_called_once_with(
        "gitea-runner", timeout_s=120.0
    )


# ----------------------------------------------------------- chart sanity


def test_chart_yaml_pins_runner_image_version() -> None:
    """The chart's appVersion must match the runner image
    version we install.
    """
    chart_yaml = Path("infra/charts/gitea-runner/Chart.yaml")
    if not chart_yaml.exists():
        pytest.skip("chart not in this tree (running from package)")
    text = chart_yaml.read_text()
    assert APP_VERSION in text
    assert CHART_VERSION in text


def test_chart_has_required_templates() -> None:
    chart_templates = Path("infra/charts/gitea-runner/templates")
    if not chart_templates.exists():
        pytest.skip("chart templates not in this tree")
    for f in ("deployment.yaml", "secret.yaml", "serviceaccount.yaml"):
        assert (chart_templates / f).exists(), f"missing template {f}"


# ----------------------------------------------------------- test isolation


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod

    # First import — registers both.
    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    # Second import — must reset first or register() complains.
    reset_registry()
    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    yield
    reset_registry()
