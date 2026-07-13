"""WP4 tests — gitea-runner app + owned chart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps.gitea_runner import (
    APP_VERSION,
    CHART_VERSION,
    GiteaRunnerApp,
    NAMESPACE,
    RUNNER_CONFIG_SECRET,
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


def test_gitea_runner_plan_mentions_local_chart_and_secret(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    plan = GiteaRunnerApp().plan(ctx, {})
    assert plan.app_name == "gitea-runner"
    assert any("helm upgrade --install gitea-runner" in s for s in plan.would_install)
    assert any(RUNNER_CONFIG_SECRET in s for s in plan.would_apply)
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

    def fake_apply(*args: object, **kwargs: object) -> MagicMock:
        return MagicMock(returncode=0, stdout="", stderr="")

    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    kubectl_mock.apply = MagicMock(side_effect=fake_apply)
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(returncode=0, stdout="gitea-runner-config", stderr="")
    )
    kubectl_mock.wait_deployments_available = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    result = GiteaRunnerApp().apply(ctx, {})

    # helm was called with the local chart path, not an OCI URL.
    helm_calls = [
        c for c in fake_run.call_args_list if "chart" in c.kwargs
    ]
    assert len(helm_calls) >= 1, f"expected helm call; got {len(helm_calls)}"
    args, kwargs = helm_calls[0]
    assert str(kwargs["chart"]).endswith("/infra/charts/gitea-runner")
    assert kwargs["namespace"] == NAMESPACE
    assert kwargs["release"] == "gitea-runner"

    # The runner-config Secret is owned by VaultwardenK8sSync.
    # The apply uses a regression guard: it inspects the
    # existing value via kubectl get and only re-seeds a
    # placeholder when the Secret is missing OR still carries
    # the chart's placeholder. A VKS-populated value is
    # left alone (the apply path that takes that branch
    # never calls kubectl.apply).
    secret_get_calls = [
        c for c in kubectl_mock.get.call_args_list
        if c.kwargs.get("name") == RUNNER_CONFIG_SECRET
    ]
    assert len(secret_get_calls) >= 1, (
        f"expected a kubectl get for {RUNNER_CONFIG_SECRET}; "
        f"got: {kubectl_mock.get.call_args_list!r}"
    )
    # The mocked kubectl.get returns an empty string for
    # the registrationToken field, which decodes to "",
    # which is NOT the placeholder string — so the apply
    # branch seeds the placeholder. That's the expected
    # first-install behavior.
    secret_apply_calls = [
        c for c in kubectl_mock.apply.call_args_list
        if RUNNER_CONFIG_SECRET in str(c)
    ]
    assert len(secret_apply_calls) >= 1, (
        f"expected a kubectl apply to seed the placeholder; "
        f"got: {kubectl_mock.apply.call_args_list!r}"
    )
    assert result.app_name == "gitea-runner"
    assert result.namespace == "gitea-runner"


def test_gitea_runner_apply_does_not_overwrite_vks_populated_token(
    tmp_path: Path,
) -> None:
    """Regression guard: when VaultwardenK8sSync has already
    written a real registrationToken to the Secret, the apply
    must NOT clobber it with a placeholder.
    """
    import base64

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

    fake_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )

    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    ctx.helm = helm_mock

    # VKS has populated the Secret with a real token.
    real_token = "real-gitea-runner-registration-token-from-vaultwarden"
    populated = base64.b64encode(real_token.encode()).decode()
    kubectl_mock = MagicMock()
    kubectl_mock.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(
            returncode=0, stdout=populated, stderr=""
        )
    )
    kubectl_mock.wait_deployments_available = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    GiteaRunnerApp().apply(ctx, {})

    # The apply must NOT have called kubectl apply for the
    # Secret — VKS is the single writer.
    secret_apply_calls = [
        c for c in kubectl_mock.apply.call_args_list
        if RUNNER_CONFIG_SECRET in str(c)
    ]
    assert secret_apply_calls == [], (
        f"apply must not overwrite a VKS-populated token; "
        f"got: {secret_apply_calls!r}"
    )


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
