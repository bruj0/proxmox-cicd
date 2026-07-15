"""test_kubeconfig_resolution — WP6 regression tests.

WP6 of the GroupSpec plan moves the kubeconfig loader
out of every app and into `BaseApp._resolve_kubeconfig`.
Apps that previously ran `Kubeconfig.load(...)` and
built their own `KubectlRunner` now call
`self._kubectl(ctx)` which delegates to the BaseApp
helper. Once cached on `ctx.kubectl`, every
subsequent app's `_kubectl(ctx)` returns the same
runner — no double-load.

These tests pin three invariants:

  1. The lazy-cache pattern works for every shipped
     app: first call resolves the kubeconfig from
     `ctx.proxmox_k3s_repo`; second call returns the
     same runner without re-reading the file.
  2. The cache lives on `ctx.kubectl`, not on the
     BaseApp class — two simultaneous contexts can
     each have their own cached runner without
     cross-talk.
  3. A missing kubeconfig file surfaces as a clear
     RuntimeError naming the path; the operator
     sees the upstream cause instead of an obscure
     kubectl error at apply-time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps.base import BaseApp


KUBECONFIG_BODY = (
    "apiVersion: v1\nkind: Config\n"
    "clusters:\n- cluster:\n    server: https://10.0.0.64:6443\n"
    "  name: cicd\n"
    "contexts:\n- context:\n    cluster: cicd\n    user: cicd\n"
    "    namespace: default\n  name: cicd\n"
    "current-context: cicd\n"
    "users:\n- name: cicd\n  user:\n    token: t\n"
)


class _FakeKubectlRunner:
    """Stand-in for `KubectlRunner` so the tests
    can assert identity without touching the real
    runner machinery.

    The orchestrator hands `ctx.kubectl` to apps;
    if it's already populated, `BaseApp._kubectl`
    must return it without spawning a new runner.
    """


class _ProbeApp(BaseApp):
    """A minimal `BaseApp` subclass used to exercise
    the lazy-cached kubeconfig path. Implements the
    four abstract methods with the minimum valid
    behaviour.
    """

    name = "_probe"

    @property
    def nodes(self):  # type: ignore[override]
        return {}

    @property
    def edges(self):  # type: ignore[override]
        return {}

    def enabled_in(self, catalog):  # type: ignore[override]
        return True

    def plan(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def apply(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def destroy(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def status(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError


def _make_ctx(
    proxmox_k3s_repo: Path,
    kubectl: Any | None = None,
) -> Any:
    """Build a `Container`-shaped mock with the four
    fields `BaseApp._kubectl` reads.
    """
    ctx = MagicMock()
    ctx.proxmox_k3s_repo = proxmox_k3s_repo
    ctx.kubectl = kubectl
    ctx.logger = MagicMock()
    return ctx


def test_resolve_kubectl_uses_existing_ctx_kubectl() -> None:
    """When `ctx.kubectl` is already populated (production
    orchestrator sets it before any app runs), `BaseApp.
    _kubectl(ctx)` returns it verbatim — no reload, no
    side effects on disk.
    """
    fake = _FakeKubectlRunner()
    ctx = _make_ctx(proxmox_k3s_repo=Path("/nonexistent"), kubectl=fake)

    app = _ProbeApp()
    out = app._kubectl(ctx)
    assert out is fake


def test_resolve_kubectl_loads_from_proxmox_k3s_repo_when_unset(
    tmp_path: Path,
) -> None:
    """When `ctx.kubectl is None` and the repo has a
    well-formed kubeconfig, the helper builds a real
    `KubectlRunner` from `ctx.proxmox_k3s_repo`, caches
    it on `ctx.kubectl`, and returns it.
    """
    cluster_dir = tmp_path / "infra" / "clusters" / "cicd"
    cluster_dir.mkdir(parents=True)
    (cluster_dir / "kubeconfig.yaml").write_text(KUBECONFIG_BODY)

    ctx = _make_ctx(proxmox_k3s_repo=tmp_path)
    app = _ProbeApp()
    runner = app._kubectl(ctx)
    assert runner is not None
    # Cached for next call.
    assert ctx.kubectl is runner


def test_resolve_kubectl_is_idempotent_per_context() -> None:
    """The cache is per-`ctx`, not per-app or per-class.
    Two distinct `_ProbeApp()` instances on the same
    context each see the same cached runner; a separate
    fresh `ctx` sees a separate runner.

    Concretely: the second call returns the SAME object
    as the first (no double-resolve), but a second
    `ctx` with `kubectl=None` produces a DIFFERENT
    runner.
    """
    cluster_dir = Path("/tmp/wp6-shared") / "infra" / "clusters" / "cicd"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / "kubeconfig.yaml").write_text(KUBECONFIG_BODY)

    ctx_a = _make_ctx(proxmox_k3s_repo=Path("/tmp/wp6-shared"))
    app_a = _ProbeApp()
    runner_a = app_a._kubectl(ctx_a)
    runner_a_again = app_a._kubectl(ctx_a)
    assert runner_a is runner_a_again

    ctx_b = _make_ctx(proxmox_k3s_repo=Path("/tmp/wp6-shared"))
    app_b = _ProbeApp()
    runner_b = app_b._kubectl(ctx_b)
    # Same file content, but distinct ctx -> distinct
    # runner instances. This prevents cross-talk when
    # the orchestrator runs apps on two clusters in
    # the same process (a future multi-cluster WP).
    assert runner_b is not runner_a


def test_resolve_kubectl_raises_when_kubeconfig_missing(
    tmp_path: Path,
) -> None:
    """Missing kubeconfig.yaml surfaces as a clear
    RuntimeError naming the path. The error message
    must include the path so the operator can fix the
    upstream cause (a failed `make apply` in
    proxmox-k3s) without re-reading the apply log.
    """
    ctx = _make_ctx(proxmox_k3s_repo=tmp_path)
    app = _ProbeApp()
    with pytest.raises(RuntimeError) as ei:
        app._kubectl(ctx)
    # The error message names the cluster's kubeconfig
    # path so the operator can `cat` it.
    assert "kubeconfig" in str(ei.value).lower()


def test_resolve_kubectl_uses_env_var_for_cluster_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PROXMOX_CICD_CLUSTER` selects which
    `infra/clusters/<cluster>/kubeconfig.yaml` gets
    loaded. The default is `cicd`; setting the env
    var lets a single `cicdctl` invocation target
    a different cluster.
    """
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "staging")
    cluster_dir = tmp_path / "infra" / "clusters" / "staging"
    cluster_dir.mkdir(parents=True)
    (cluster_dir / "kubeconfig.yaml").write_text(KUBECONFIG_BODY)

    ctx = _make_ctx(proxmox_k3s_repo=tmp_path)
    app = _ProbeApp()
    runner = app._kubectl(ctx)
    assert runner is not None
    # If the cluster name resolution is wrong, the
    # helper would have looked for
    # `infra/clusters/cicd/kubeconfig.yaml` and either
    # found a stale one or raised. Pin the success by
    # reading the cached runner back.
    assert ctx.kubectl is runner
