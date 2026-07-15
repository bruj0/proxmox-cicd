"""WP3 tests — AppSpec protocol, registry, GiteaApp."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps import (
    AppApplyResult,
    all_apps,
    app_by_name,
    register,
    reset_registry,
)
from provisioner.lib.apps.gitea import (
    CHART_VERSION,
    NAMESPACE,
    GiteaApp,
)
from provisioner.lib.container import Container


# ----------------------------------------------------------- registry mechanics


def test_register_rejects_class_without_name() -> None:
    reset_registry()

    class Nameless:
        def plan(self, ctx, catalog): ...
        def apply(self, ctx, catalog): ...
        def destroy(self, ctx, catalog): ...
        def status(self, ctx, catalog): ...

    with pytest.raises(TypeError) as ei:
        register(Nameless)  # type: ignore[arg-type]
    assert "must define a non-empty `name`" in str(ei.value)


def test_register_rejects_duplicate_name() -> None:
    reset_registry()

    @register
    class A:
        name = "dup-app"

        def plan(self, ctx, catalog): ...
        def apply(self, ctx, catalog): ...
        def destroy(self, ctx, catalog): ...
        def status(self, ctx, catalog): ...

    with pytest.raises(ValueError) as ei:

        @register
        class B:  # noqa: F811
            name = "dup-app"

            def plan(self, ctx, catalog): ...
            def apply(self, ctx, catalog): ...
            def destroy(self, ctx, catalog): ...
            def status(self, ctx, catalog): ...

    assert "already registered" in str(ei.value)


def test_all_apps_returns_registration_order() -> None:
    reset_registry()

    @register
    class One:
        name = "one"

        def plan(self, ctx, catalog): ...
        def apply(self, ctx, catalog): ...
        def destroy(self, ctx, catalog): ...
        def status(self, ctx, catalog): ...

    @register
    class Two:
        name = "two"

        def plan(self, ctx, catalog): ...
        def apply(self, ctx, catalog): ...
        def destroy(self, ctx, catalog): ...
        def status(self, ctx, catalog): ...

    assert [a.name for a in all_apps()] == ["one", "two"]
    assert app_by_name("one") is One
    assert app_by_name("missing") is None


def test_gitea_app_is_registered_on_import() -> None:
    """Importing apps.gitea auto-registers GiteaApp."""
    import importlib

    reset_registry()  # autouse already did, but be explicit
    from provisioner.lib.apps import gitea as gitea_mod

    importlib.reload(gitea_mod)
    assert app_by_name("gitea") is gitea_mod.GiteaApp
    assert any(a.name == "gitea" for a in all_apps())


# ----------------------------------------------------------- GiteaApp.plan


def test_gitea_plan_returns_helm_install_and_httproute(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "values").mkdir(parents=True)
    (repo / "values" / "gitea.yaml").write_text("# placeholder\n")
    ctx = _make_ctx(repo)
    catalog = {"ingress": {"base_domain": "example.net"}}

    result = GiteaApp().plan(ctx, catalog)
    assert result.app_name == "gitea"
    assert any("helm upgrade --install gitea" in s for s in result.would_install)
    assert any("Gateway=gitea" in s for s in result.would_apply)
    assert "gitea.example.net" in str(result.notes)


# ----------------------------------------------------------- GiteaApp.apply


def test_gitea_apply_runs_helm_then_kubectl(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "values").mkdir(parents=True)
    # Commit-style values file with the same `$PUBLIC_HOST` /
    # `$PROTOCOL` sentinels the real `values/gitea.yaml`
    # carries. The apply step must rewrite these into the
    # rendered sibling before passing it to helm — that's
    # the whole point of the rendered-values convention.
    (repo / "values" / "gitea.yaml").write_text(
        "gitea:\n"
        "  config:\n"
        "    server:\n"
        "      ROOT_URL: 'https://$PUBLIC_HOST/'\n"
        "      DOMAIN: '$PUBLIC_HOST'\n"
        "      SSH_DOMAIN: '$PUBLIC_HOST'\n"
        "      PROTOCOL: 'http'\n"
        "    service:\n"
        "      DISABLE_REGISTRATION: true\n"
    )
    # Lay down a kubeconfig the loader can parse. The gitea
    # app reads it from <proxmox_k3s_repo>/infra/clusters/cicd/
    # so we put it there.
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")

    ctx = _make_ctx(repo)
    catalog = {
        "ingress": {"base_domain": "example.net"},
        # Skip the Vaultwarden seed step in this test
        # (covered by its own dedicated test). Without
        # this the apply path would try to read the
        # BW_CLIENTID/BW_CLIENTSECRET Secret + log in to
        # a real Vaultwarden, neither of which the unit
        # test mocks.
        "vaultwarden": {"skip_admin_seed": True},
    }

    # Mock helm + kubectl.
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = fake_run
    helm_mock.uninstall = fake_run
    ctx.helm = helm_mock
    kubectl_mock = MagicMock()
    kubectl_mock.apply = fake_run
    kubectl_mock.delete_namespace = fake_run
    # The admin-pw lifecycle in apply step 1b reads the
    # live `gitea-admin-password` Secret via kubectl.get
    # (drift-check) AND the VKS Secret (Vaultwarden
    # creds) via kubectl.get. Pretend both calls return
    # "not found" so the apply proceeds to `kubectl apply`
    # (which is also fake_run). kubectl.get returncode != 0
    # simulates "not found" without making the mock return
    # a real base64 string.
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="not found")
    )
    ctx.kubectl = kubectl_mock

    result = GiteaApp().apply(ctx, catalog)

    # 1. helm was called with the right args. fake_run
    #    is shared between helm_mock.install_or_upgrade
    #    and kubectl_mock.apply. With the admin-pw
    #    lifecycle in front of helm, the call order is:
    #      [0] kubectl apply Secret=gitea-admin-password
    #      [1] helm install_or_upgrade gitea
    #      [2] kubectl apply Gateway/HTTPRoute
    #    We locate the helm call by `release=gitea` since
    #    the kubectl calls don't carry that kwarg.
    helm_call = next(
        c for c in fake_run.call_args_list
        if c.kwargs.get("release") == "gitea"
    )
    args, kwargs = helm_call
    assert kwargs["chart"].startswith("oci://")
    assert kwargs["namespace"] == "gitea"
    assert kwargs["version"] == CHART_VERSION
    # Apply renders the orchestrator-injected hostname into a
    # sibling `gitea.values-rendered.yaml` (the committed
    # `gitea.yaml` carries `$PUBLIC_HOST`/`$PROTOCOL`
    # sentinels). Helm must consume the rendered file, not
    # the committed defaults — see GiteaApp.apply docstring.
    assert kwargs["values_files"][0].name == "gitea.values-rendered.yaml"

    # 2. Two kubectl.apply calls (cluster Secret +
    #    Gateway/HTTPRoute) + 1 helm install = 3 total
    #    calls to fake_run. (kubectl.get goes through
    #    kubectl_mock.get, not fake_run.)
    assert fake_run.call_count == 3

    # 3. The rendered sibling actually exists on disk, with
    #    the sentinels substituted. This is what would
    #    otherwise regress to "Your ROOT_URL in app.ini is
    #    unlikely matching the site you are visiting" at
    #    every Gitea boot — the symptom that motivated the
    #    rendered-values convention.
    rendered = repo / "values" / "gitea.values-rendered.yaml"
    assert rendered.exists()
    text = rendered.read_text()
    assert "$PUBLIC_HOST" not in text
    assert "gitea.example.net" in text
    assert "https" in text

    # 4. The result has the right metadata.
    assert isinstance(result, AppApplyResult)
    assert result.app_name == "gitea"
    assert result.namespace == "gitea"
    assert result.ingress_host == "gitea.example.net"


def test_gitea_apply_fails_when_values_missing(tmp_path: Path) -> None:
    repo = tmp_path  # no values/ dir
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    catalog: dict = {}
    with pytest.raises(FileNotFoundError) as ei:
        GiteaApp().apply(ctx, catalog)
    assert "values/gitea.yaml" in str(ei.value)


def test_gitea_apply_fails_when_helm_fails(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "values").mkdir(parents=True)
    (repo / "values" / "gitea.yaml").write_text("# placeholder\n")
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    fake = MagicMock(return_value=MagicMock(returncode=1, stderr="boom"))
    ctx.helm = MagicMock(install_or_upgrade=fake, uninstall=fake)

    # Admin-pw lifecycle (step 1b) runs before helm and
    # needs kubectl.get mocked (drift-check + VKS-cred
    # read) + kubectl.apply mocked (Secret write). Both
    # return success; the test still asserts the helm
    # failure bubbles up because helm is what the test
    # cares about.
    kubectl_mock = MagicMock()
    kubectl_mock.get = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="not found")
    )
    kubectl_mock.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.kubectl = kubectl_mock

    with pytest.raises(RuntimeError) as ei:
        GiteaApp().apply(
            ctx,
            {
                "ingress": {"base_domain": "x"},
                "vaultwarden": {"skip_admin_seed": True},
            },
        )
    assert "helm upgrade --install gitea failed" in str(ei.value)


# ----------------------------------------------------------- GiteaApp.status


def test_gitea_status_when_release_present(tmp_path: Path) -> None:
    repo = tmp_path
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="gitea\tgitea\t1\t...", stderr="")
    )
    ctx.helm = MagicMock(list_releases=fake)

    status = GiteaApp().status(ctx, {"ingress": {"base_domain": "x"}})
    assert status.app_name == "gitea"
    assert status.namespace == NAMESPACE
    assert status.release_present is True
    assert status.ingress_host == "gitea.x"


def test_gitea_status_when_release_missing(tmp_path: Path) -> None:
    repo = tmp_path
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    _write_kubeconfig(k8s / "kubeconfig.yaml")
    ctx = _make_ctx(repo)
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    ctx.helm = MagicMock(list_releases=fake)

    status = GiteaApp().status(ctx, {})
    assert status.release_present is False
    assert status.ingress_host is None
    assert any("not installed" in n for n in status.notes)


# ----------------------------------------------------------- helpers


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


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a fresh registry (except the
    gitea app which self-registers on import and remains
    visible — that's the point of the test_gitea_app_is_
    registered_on_import test). Also pins the env var the
    gitea app reads to find the cluster name."""
    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    # Re-import gitea to register it again.
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod

    importlib.reload(gitea_mod)
    yield
    reset_registry()
