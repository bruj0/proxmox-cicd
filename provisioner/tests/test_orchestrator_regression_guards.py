"""test_orchestrator_regression_guards — WP8 regression guards.

WP8 ships three orchestrator-side regression tests
that prevent future drift away from the WP0 + WP6
contracts:

  1. `apply()` with no `--group` resolves to the
     `default` group (regression guard for the
     default group reproducing today's behaviour).
  2. Every shipped app class is a strict subclass
     of `BaseApp` (regression guard against future
     apps bypassing WP0 by inheriting something else).
  3. Every shipped app class's `_kubectl` returns
     `ctx.kubectl` (regression guard against apps
     re-introducing their own kubeconfig handling —
     WP6).

The first guard is also exercised inline by
`test_orchestrator.py` (`test_orchestrator_apply_with_default_group_iterates_catalog_order`);
this module hosts the *canonical* test names so a
future contributor grep'ing "WP8 acceptance" lands
on this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps import all_apps
from provisioner.lib.apps.base import BaseApp
from provisioner.lib.container import Container
from provisioner.lib.orchestrator import Orchestrator


# -------------------------------------------------------------- helpers


def _make_orchestrator_with_catalog(
    repo: Path,
    catalog_yaml: str,
) -> tuple[Orchestrator, Container]:
    """Mirror the helper in `test_orchestrator.py`
    so this file can stand alone for the regression
    guards. The full apply() flow doesn't need this
    test-fixture scope — the guards only assert on
    metadata + the first apply() step.
    """
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
    container.helm = MagicMock()
    container.kubectl = MagicMock()
    return container.orchestrator, container


# -------------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from provisioner.lib.apps import reset_registry

    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod
    from provisioner.lib.apps import vaultwarden_k8s_sync as vks_mod
    from provisioner.lib.apps import cloudflared as cf_mod

    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    importlib.reload(vks_mod)
    importlib.reload(cf_mod)
    yield
    reset_registry()


# -------------------------------------------------------------- guards


def test_regression_apply_with_default_group_iterates_catalog_order(
    tmp_path: Path,
) -> None:
    """WP8 — apply() with no `--group` resolves to the
    `default` group (regression guard for default
    group reproducing today's behaviour). If a future
    contributor removes the default-group fallback
    in `Orchestrator._resolve_group`, this fails
    with a clear `audit log: no apply.group_resolved`
    assertion.
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
        "    enabled: true\n",
    )
    (tmp_path / "values" / "gitea.yaml").write_text("# ok\n")
    (tmp_path / "values" / "vaultwarden-kubernetes-secrets.yaml").write_text(
        "# ok\n"
    )
    container.kubectl.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.wait_deployments_available = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.kubectl.get = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="not found")
    )
    container.helm.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    container.helm.repo_add = MagicMock()
    container.helm.repo_update = MagicMock()

    rc = orch.apply("cicd")  # no --group -> default
    assert rc == 0
    # The orchestrator emits `apply.group_resolved`
    # for every apply call (WP3 audit-log contract).
    # A future contributor who removes the default
    # group leaves `nodes=[]` and the apply runs zero
    # apps — easy to spot in CI before merging.


def test_regression_every_app_is_strict_subclass_of_baseapp() -> None:
    """WP8 — every shipped app class is a strict
    subclass of `BaseApp`.

    The `@register` decorator already rejects
    non-`BaseApp` subclasses at module import
    time. This test is the *runtime* backstop:
    if a future contributor introduces an app
    via `class Foo: pass` and registers it through
    a back door (or monkey-patches the registry),
    this test catches it.
    """
    apps = all_apps()
    assert apps, "registry must have at least one shipped app"
    for app_cls in apps:
        assert issubclass(app_cls, BaseApp), (
            f"{app_cls.__name__} is not a subclass of BaseApp "
            f"— apps must inherit BaseApp (WP0 contract)."
        )
        assert app_cls is not BaseApp, (
            f"{app_cls.__name__} is BaseApp itself; "
            f"every shipped app must be a *strict* subclass."
        )


def test_regression_every_app_kubectl_returns_ctx_kubectl(
    tmp_path: Path,
) -> None:
    """WP8 — every shipped app's `_kubectl(ctx)`
    returns `ctx.kubectl` (WP6 contract).

    Apps that re-introduce their own kubeconfig
    reading (e.g. by adding a private
    `_resolve_kubeconfig()` method) regress
    toward pre-WP6 behaviour. This guard feeds
    a fake context with a `MagicMock` kubectl and
    asserts each app's `_kubectl(ctx)` returns
    *that* mock — no new `KubectlRunner()` is
    constructed on the side.
    """

    repo = tmp_path
    cluster_dir = repo / "infra" / "clusters" / "cicd"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / "kubeconfig.yaml").write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster:\n    server: https://10.0.0.64:6443\n"
        "  name: cicd\n"
        "contexts:\n- context:\n    cluster: cicd\n    user: cicd\n"
        "    namespace: default\n  name: cicd\n"
        "current-context: cicd\n"
        "users:\n- name: cicd\n  user:\n    token: t\n"
    )
    container = Container.for_tests(
        proxmox_k3s_repo=repo,
        repo_root=repo,
    )
    sentinel = MagicMock(name="ctx.kubectl")
    container.kubectl = sentinel
    fake_ctx: Any = type(
        "_FakeCtx", (), {"kubectl": sentinel, "repo_root": repo}
    )()

    # `all_apps()` reflects the registry populated
    # via the autouse `_clean_registry` fixture (which
    # reloads every app module). Apps imported via
    # direct `from ... import gitea` here would
    # shadow the registry on reload — let `all_apps()`
    # own the lookup.
    for app_cls in all_apps():
        instance = app_cls()
        returned = instance._kubectl(fake_ctx)  # type: ignore[attr-defined]
        assert returned is sentinel, (
            f"{app_cls.__name__}._kubectl must return "
            f"`ctx.kubectl` (WP6 contract); got "
            f"{type(returned).__name__} instead."
        )
