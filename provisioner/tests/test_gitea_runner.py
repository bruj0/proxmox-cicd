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
    # Plan must call out the non-ephemeral + dind + StatefulSet
    # choices; these are the key behavioural differences from
    # the legacy Deployment + ephemeral runner shape.
    notes_blob = "\n".join(plan.notes)
    assert "ephemeral: false" in notes_blob, (
        f"plan must explain the non-ephemeral mode; notes: {plan.notes!r}"
    )
    assert "StatefulSet" in notes_blob, (
        f"plan must explain the StatefulSet shape; notes: {plan.notes!r}"
    )
    assert "dind" in notes_blob, (
        f"plan must call out the dind image flavour; notes: {plan.notes!r}"
    )
    assert "/healthz" in notes_blob, (
        f"plan must call out the /healthz probe path; notes: {plan.notes!r}"
    )


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
        # Empty / not-found Secret → exercise the
        # placeholder-write branch. The chart's Secret
        # name is gitea-runner-gitea-runner-config but we
        # deliberately mock an empty lookup here so the
        # orchestrator takes the apply-write path that
        # the test asserts against.
        return_value=MagicMock(returncode=1, stdout="", stderr="not found"),
    )
    kubectl_mock.wait = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    result = GiteaRunnerApp().apply(
        ctx, {"vaultwarden": {"skip_runner_seed": True}}
    )

    # The orchestrator waits for the StatefulSet to become
    # Available (not a Deployment — the chart is a
    # StatefulSet, see infra/charts/gitea-runner/templates/
    # statefulset.yaml). Asserting the wait() call shape
    # is the contract that prevents silent regressions
    # back to the legacy Deployment-only code path.
    wait_calls = [
        c for c in kubectl_mock.wait.call_args_list
        if c.kwargs.get("resource") == "statefulset"
    ]
    assert wait_calls, (
        "apply must wait for the StatefulSet to become "
        "Available; got: "
        f"{kubectl_mock.wait.call_args_list!r}"
    )
    wait_kwargs = wait_calls[0].kwargs
    assert wait_kwargs["name"] == "gitea-runner"
    assert wait_kwargs["namespace"] == NAMESPACE
    assert "Available" in wait_kwargs["condition"]

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
    kubectl_mock.wait = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    GiteaRunnerApp().apply(
        ctx, {"vaultwarden": {"skip_runner_seed": True}}
    )

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


def test_gitea_runner_apply_is_no_op_when_cipher_already_seeded(
    tmp_path: Path,
) -> None:
    """Full idempotency at the network level: when Vaultwarden
    already carries a Secure Note matching the runner's VKS
    triple, the apply must NOT mint a fresh token via the
    Gitea admin API and must NOT POST a new cipher. Both calls
    leave a side-effect (Gitea increments its registration-token
    counter; Vaultwarden dedup would have to fire) and are
    skipped here.

    Regression guard for the bug where token-minted fired on
    every apply even though vaultwarden-seeded was correctly
    skipped.
    """
    import unittest.mock as _um

    from provisioner.lib.vaultwarden import (
        VaultwardenClient as RealClient,
    )

    repo = tmp_path
    chart_dir = repo / "infra" / "charts" / "gitea-runner"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: gitea-runner\nversion: 0.1.0\n"
    )
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")

    # .env with the canonical Vaultwarden master-pw key, so
    # _read_dotenv_creds resolves without raising.
    (repo / ".env").write_text(
        "VAULTWARDEN__MASTERPASSWORD=test\nBW_CLIENTID=test\n"
        "BW_CLIENTSECRET=test\nclient_email=ops@example.org\n"
    )

    ctx = _make_ctx(repo)
    fake_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    # The runner Secret *is* populated (real token), so the
    # orchestrator's placeholder-write branch must not run.
    kubectl_mock.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout="Y3VycmVudC1pbnN0YWxsZWQtdG9rZW4=",  # base64("current-installed-token")
            stderr="",
        )
    )
    kubectl_mock.wait = fake_run
    kubectl_mock.delete_namespace = fake_run
    ctx.kubectl = kubectl_mock

    # Stub client: list_ciphers returns ONE existing cipher
    # that already carries the runner's exact triple. The
    # orchestrator's decrypt-cipher-field helpers always
    # succeed (return a string), so the matching branch fires.
    existing_cipher = {
        "id": "stale-runner-cipher-id",
        "type": 2,
        "fields": [
            {"name": "enc(name1)", "value": "enc(value1)"},
            {"name": "enc(name2)", "value": "enc(value2)"},
            {"name": "enc(name3)", "value": "enc(value3)"},
        ],
    }
    stub = MagicMock()
    stub.user_key = b"k" * 64
    stub.list_ciphers = MagicMock(return_value=[existing_cipher])
    stub.decrypt_cipher_field_name = MagicMock(
        side_effect=lambda c, index: [
            "namespaces", "secret-name", "secret-key"
        ][index]
    )
    stub.decrypt_cipher_field = MagicMock(
        side_effect=lambda c, name: {
            "namespaces": "gitea-runner",
            "secret-name": "gitea-runner-gitea-runner-config",
            "secret-key": "registrationToken",
        }[name]
    )
    stub.create_cipher = MagicMock()  # must NOT be called
    # Stub _fetch_gitea_admin_from_vaultwarden too: it
    # already returns a client + creds and the orchestrator
    # reuses the client. Patch that and patch the Gitea
    # admin-API call to assert it's NOT hit on re-apply.
    admin_creds = ("gitea_admin", "test-pw", stub)
    fetch_stub = MagicMock(return_value=admin_creds)
    with _um.patch.object(RealClient, "login", classmethod(lambda cls, **kw: stub)), \
         _um.patch.object(
             GiteaRunnerApp,
             "_fetch_gitea_admin_from_vaultwarden",
             new=fetch_stub,
         ), \
         _um.patch.object(
             GiteaRunnerApp,
             "_mint_runner_token_from_gitea_api",
         ) as mint_mock:
        GiteaRunnerApp().apply(ctx, {"vaultwarden": {}})

    # The Gitea admin API must NOT have been called — the
    # apply is a no-op at the network level.
    mint_mock.assert_not_called()
    # The vaultwarden POST must NOT have happened.
    stub.create_cipher.assert_not_called()


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


def test_chart_yaml_documents_dind_choice() -> None:
    """Regression guard: the Chart.yaml description must
    explain why we picked dind (root) over dind-rootless.
    If someone flips the image tag without updating this
    comment, they will hit the rootlesskit
    `fork/exec /proc/self/exe: operation not permitted`
    error and not understand why.
    """
    chart_yaml = Path("infra/charts/gitea-runner/Chart.yaml")
    if not chart_yaml.exists():
        pytest.skip("chart not in this tree (running from package)")
    text = chart_yaml.read_text()
    assert "dind-rootless" in text, (
        "Chart.yaml must document why we did NOT pick "
        "dind-rootless; otherwise the next reader will flip "
        "the image tag and hit the rootlesskit failure"
    )
    assert "fork/exec" in text or "operation not permitted" in text, (
        "Chart.yaml must include the actual error message "
        "that rootless DinD produces, so future operators "
        "can recognise it from a chart search"
    )


def test_chart_has_required_templates() -> None:
    chart_templates = Path("infra/charts/gitea-runner/templates")
    if not chart_templates.exists():
        pytest.skip("chart templates not in this tree")
    for f in ("statefulset.yaml", "secret.yaml", "serviceaccount.yaml"):
        assert (chart_templates / f).exists(), f"missing template {f}"


def test_statefulset_template_has_persistent_registrations() -> None:
    """The chart's StatefulSet must:
      * persist /data so the .runner registration file
        survives pod restarts (otherwise every pod start
        would insert a new action_runner row in Gitea);
      * persist /var/lib/docker so the image cache
        survives pod restarts;
      * probe /healthz on the metrics port (the runner
        daemon's real health-check endpoint, not /-/ready
        which doesn't exist in gitea-runner 1.0.8);
      * mount a config.yaml so the daemon's metrics
        endpoint is actually enabled.
    """
    sset = Path(
        "infra/charts/gitea-runner/templates/statefulset.yaml"
    )
    if not sset.exists():
        pytest.skip("statefulset template not in this tree")
    text = sset.read_text()
    assert "kind: StatefulSet" in text
    assert "volumeClaimTemplates" in text, (
        "StatefulSet must own its PVCs via "
        "volumeClaimTemplates so each replica gets a "
        "stable identity"
    )
    assert "/data" in text, (
        "/data mount is required for the .runner file "
        "(re-attachment state)"
    )
    assert "/var/lib/docker" in text, (
        "/var/lib/docker mount is required for the "
        "bundled dockerd's image cache"
    )
    assert "/healthz" in text, (
        "the runner daemon's actual health-check "
        "endpoint is /healthz (not /-/ready); see "
        "internal/pkg/metrics/server.go in gitea-runner"
    )
    assert "metrics" in text, (
        "container port for /healthz must be named "
        "'metrics' (was previously named 'cache' before "
        "we discovered the cache port isn't actually "
        "served by the daemon)"
    )
    assert "runner-config" in text, (
        "ConfigMap mount providing config.yaml is "
        "required to enable the metrics endpoint via "
        "metrics.enabled: true"
    )


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
