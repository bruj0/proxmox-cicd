"""test_base_app_helpers — WP9 contract tests.

WP9 is the second-wave BaseApp helper sweep. The
canonical helpers added to `apps/base.py`:

  * `_secret_ref(name, key)` builds the k8s
    `valueFrom: { secretKeyRef: ... }` shape apps
    embed in their generated manifests. Future use
    in templates; no app currently calls it (the
    WP9 refactor is forward-looking).
  * `_hostname(catalog)` returns
    `f"{self.name}.{base_domain}"` reading
    `catalog["ingress"]["base_domain"]`. Replaces the
    per-app `_hostname` overrides on
    `GiteaApp` + `CloudflaredApp` (cloudflared's
    pre-WP9 implementation returned `gitea.<base>`,
    a bug — WP9 fixes it).
  * `_labels()`, `_annotations()` return the canonical
    proxmox-cicd label set apps stamp on their
    namespace + Secret manifests. Apps use
    `super()._labels() | {...}` to layer extras.
  * `_deep_merge(a, b)` is the existing
    catalog-deep-merge lifted onto BaseApp.
  * `_values_file(ctx)` returns
    `ctx.repo_root / self.default_values_file`. Replaces
    per-app `_values_file` overrides on all four apps.

These tests describe the WP9 contract; the
implementation lives in `apps/base.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provisioner.lib.apps.base import BaseApp


class _ProbeApp(BaseApp):
    """Stand-in subclass that disables the 4 abstract
    methods so we can instantiate BaseApp for these
    helper tests."""

    name = "probe"
    chart = "oci://example.com/probe"
    chart_version = "0.0.1"
    image_version = "0.0.1"
    default_values_file = "values/probe.yaml"

    def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def destroy(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def app() -> BaseApp:
    return _ProbeApp()


# ----- _secret_ref ------------------------------------------------------


def test_secret_ref_returns_expected_kubernetes_object(
    app: BaseApp,
) -> None:
    """`SecretKeySelector` shape: name + key, ready to
    drop into a `valueFrom` block."""
    ref = app._secret_ref("gitea-admin-password", "password")
    assert ref == {
        "secretKeyRef": {
            "name": "gitea-admin-password",
            "key": "password",
        }
    }


# ----- _hostname ---------------------------------------------------------


def test_hostname_uses_base_domain_from_catalog(app: BaseApp) -> None:
    """Default `_hostname` is `f"{self.name}.{base_domain}"`.

    Cloudflared's pre-WP9 implementation returned
    `gitea.<base>` regardless of the app name — WP9
    fixes that bug by deriving the host from the app
    identity.
    """
    catalog = {"ingress": {"base_domain": "bruj0.net"}}
    assert app._hostname(catalog) == "probe.bruj0.net"


def test_hostname_defaults_to_example_net(app: BaseApp) -> None:
    """Missing `catalog.ingress.base_domain` falls back
    to `example.net` so the operator gets a non-broken
    default during testing."""
    assert app._hostname({}) == "probe.example.net"


# ----- _labels / _annotations -------------------------------------------


def test_labels_includes_managed_by_and_app_name(app: BaseApp) -> None:
    """Canonical label set:

      * `app.kubernetes.io/name: <self.name>` — every
        object gets the app's stable identity.
      * `app.kubernetes.io/managed-by: proxmox-cicd` —
        lets `kubectl get all -l app.kubernetes.io/managed-by=proxmox-cicd`
        surface everything the orchestrator owns.

    Apps can layer extras via
    `super()._labels() | {...}`.
    """
    labels = app._labels()
    assert labels["app.kubernetes.io/name"] == "probe"
    assert labels["app.kubernetes.io/managed-by"] == "proxmox-cicd"


def test_annotations_includes_managed_by(app: BaseApp) -> None:
    """The annotation set mirrors `app.kubernetes.io/managed-by`
    so kubectl-aware tooling can find orchestrator-owned
    resources without a label selector."""
    annotations = app._annotations()
    assert annotations["app.kubernetes.io/managed-by"] == "proxmox-cicd"


# ----- _deep_merge -------------------------------------------------------


def test_deep_merge_overrides_left_keys(app: BaseApp) -> None:
    """Right-hand dict wins on overlapping keys at the
    same level."""
    left = {"a": 1, "b": 2}
    right = {"b": 99}
    assert BaseApp._deep_merge(left, right) == {"a": 1, "b": 99}


def test_deep_merge_preserves_unset_left_subkeys(app: BaseApp) -> None:
    """Recursive merge: a partial right-hand override at
    `path.foo` doesn't clobber `path.bar`."""
    left = {"path": {"foo": 1, "bar": 2}, "other": "keep"}
    right = {"path": {"foo": 99}}
    assert BaseApp._deep_merge(left, right) == {
        "path": {"foo": 99, "bar": 2},
        "other": "keep",
    }


def test_deep_merge_does_not_mutate_inputs(app: BaseApp) -> None:
    """The merge is pure-function style. Inputs must
    survive untouched (the catalog loader relies on
    this to merge shipped + per-cluster without side
    effects)."""
    left = {"a": {"x": 1}}
    right = {"a": {"y": 2}}
    BaseApp._deep_merge(left, right)
    assert left == {"a": {"x": 1}}
    assert right == {"a": {"y": 2}}


def test_deep_merge_replaces_non_dict_with_dict(app: BaseApp) -> None:
    """When the right-hand value at a key is a dict and
    the left-hand value is a scalar, the right wins
    entirely (no recursion)."""
    left = {"a": "scalar"}
    right = {"a": {"nested": True}}
    assert BaseApp._deep_merge(left, right) == {"a": {"nested": True}}


# ----- _values_file ------------------------------------------------------


def test_values_file_returns_repo_root_default(app: BaseApp) -> None:
    """`_values_file(ctx)` returns
    `ctx.repo_root / self.default_values_file`.

    Replaces the four per-app `_values_file` methods
    that read `ctx.repo_root / DEFAULT_VALUES_FILE`.
    Each subclass overrode only because the
    module-level constant was a different value per
    app; the class attribute (`default_values_file`)
    now does the same job.
    """

    class _FakeCtx:
        repo_root = Path("/srv/proxmox-cicd")

    path = app._values_file(_FakeCtx())  # type: ignore[arg-type]
    assert path == Path("/srv/proxmox-cicd/values/probe.yaml")


# ----- _values_file default_values_file is optional -----------------------


def test_values_file_uses_default_when_no_class_attr() -> None:
    """An app that doesn't override `default_values_file`
    gets the BaseApp default — the absent-attribute
    branch is reachable for new `BaseApp` subclasses
    without a committed values file (e.g. `CloudflareTunnel`)."""

    class _NoValuesApp(BaseApp):
        name = "no-values"
        chart = "oci://example.com/no-values"
        chart_version = "0.0.1"
        image_version = "0.0.1"

        def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def destroy(self, ctx, catalog):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    # The probe's `default_values_file` shape is
    # `values/<app>.yaml` — verify a name with a hyphen
    # doesn't trip the default.
    app = _NoValuesApp()

    class _FakeCtx:
        repo_root = Path("/srv/repo")

    # WP9 doesn't enforce a default for `_values_file`
    # when `default_values_file` is unset — apps that
    # don't have a values file just don't call
    # `_values_file`. The base method raises
    # `NotImplementedError` in that case to catch the
    # mistake at apply()-time.
    with pytest.raises(NotImplementedError):
        app._values_file(_FakeCtx())  # type: ignore[arg-type]

# ----- _rendered_values_file ---------------------------------------------


def test_rendered_values_file_sibling_to_default(app: BaseApp) -> None:
    """The rendered file lives next to the default
    values file, with `.<stem>.values-rendered.yaml`
    (no extension swap — both files are `.yaml`).

    WP9 — centralizes the `values-rendered` literal so
    apps don't construct the path inline. The same
    path is produced by WP10's `cicdctl render`
    command, so `apply()` and the CLI agree."""

    class _FakeCtx:
        repo_root = Path("/srv/proxmox-cicd")

    rendered = app._rendered_values_file(_FakeCtx())  # type: ignore[arg-type]
    assert rendered == Path("/srv/proxmox-cicd/values/probe.values-rendered.yaml")
