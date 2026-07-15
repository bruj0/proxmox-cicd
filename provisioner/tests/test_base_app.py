"""test_base_app — WP0 regression tests for the BaseApp ABC.

These tests pin the four invariants WP0 introduces:

  1. `BaseApp` is an `abc.ABC`; instantiating it directly
     raises `TypeError` (can't instantiate abstract class).
  2. A subclass that omits any of the four abstract
     methods cannot be instantiated either.
  3. A subclass that omits `name` raises `TypeError` at
     class-creation time, not at instantiation time.
  4. `@register` rejects a non-`BaseApp` class with a
     clear `TypeError` referencing the missing subclass.

These exist so future contributors cannot accidentally
revert the `Protocol` shape: a misspelled method name
surfaces as a `TypeError` at import time, not as an
`AttributeError` deep inside the orchestrator at apply
time.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import pytest

from provisioner.lib.apps import AppApplyResult, AppPlanResult, AppStatus, register
from provisioner.lib.apps.base import BaseApp


def test_baseapp_is_an_abc() -> None:
    """`BaseApp` is declared as an `abc.ABC`. The metaclass
    is `ABCMeta`, not the default `type`."""
    assert issubclass(BaseApp, abc.ABC)
    assert isinstance(BaseApp, abc.ABCMeta)


def test_baseapp_cannot_be_instantiated_directly() -> None:
    """Trying to do `BaseApp()` raises `TypeError` because
    the four abstract methods are unimplemented."""
    with pytest.raises(TypeError, match="Can't instantiate abstract class"):
        BaseApp()  # type: ignore[abstract]


def test_subclass_without_apply_cannot_be_instantiated() -> None:
    """A subclass that implements `plan` / `destroy` /
    `status` but NOT `apply` still cannot be instantiated.
    The ABCMeta check is per-method."""
    class IncompleteApp(BaseApp):
        name = "incomplete"

        def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppPlanResult(app_name=self.name)

        # NOTE: apply() deliberately omitted.

        def destroy(self, ctx, catalog) -> None:
            pass

        def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppStatus(
                app_name=self.name,
                namespace="",
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
            )

    with pytest.raises(TypeError, match="apply"):
        IncompleteApp()  # type: ignore[abstract]


def test_subclass_without_name_raises_at_class_creation() -> None:
    """A subclass that forgets `name` raises `TypeError`
    at the `class` line, not later. This is the "fail fast
    at import time" promise of WP0."""

    with pytest.raises(TypeError, match="must define `name`"):

        class NamelessApp(BaseApp):
            # NOTE: name deliberately omitted.

            def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
                return AppPlanResult(app_name="nameless")

            def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
                return AppApplyResult(
                    app_name="nameless",
                    namespace="",
                    release="",
                    chart_version="",
                    image_version="",
                )

            def destroy(self, ctx, catalog) -> None:
                pass

            def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
                return AppStatus(
                    app_name="nameless",
                    namespace="",
                    release_present=False,
                    chart_version=None,
                    image_version=None,
                    ingress_host=None,
                )


def test_subclass_with_all_four_methods_and_name_instantiates() -> None:
    """A complete subclass instantiates cleanly. This is
    the happy path the existing four apps follow after WP0."""

    class GoodApp(BaseApp):
        name = "good"

        def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppPlanResult(app_name=self.name)

        def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppApplyResult(
                app_name=self.name,
                namespace="",
                release="",
                chart_version="",
                image_version="",
            )

        def destroy(self, ctx, catalog) -> None:
            pass

        def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppStatus(
                app_name=self.name,
                namespace="",
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
            )

    app = GoodApp()
    assert app.name == "good"


def test_register_rejects_non_baseapp_class() -> None:
    """`@register` must refuse to register a class that
    is not a `BaseApp` subclass. The decorator surfaces
    the wrong shape as a `TypeError`, not a silent drop."""

    class NotAnApp:  # no BaseApp, no AppSpec, nothing.
        name = "not-an-app"

    with pytest.raises(TypeError, match="must subclass BaseApp"):
        register(NotAnApp)  # type: ignore[arg-type]


def test_existing_apps_subclass_baseapp() -> None:
    """The four shipped apps (`gitea`, `gitea-runner`,
    `cloudflared`, `vaultwarden-k8s-sync`) are all
    subclasses of `BaseApp` after WP0 lands. This is the
    migration-completeness regression guard.

    The `test_apps.py` autouse fixture re-imports gitea
    on every test, so at minimum `gitea` is registered
    here. The remaining three apps are registered by
    reloading their modules (the gitea import re-registered
    itself; the other three never get re-imported in the
    autouse fixture, so we explicitly reload them here).
    """
    import importlib

    from provisioner.lib.apps import app_by_name

    from provisioner.lib.apps import cloudflared as cf_mod
    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod
    from provisioner.lib.apps import vaultwarden_k8s_sync as vks_mod

    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    importlib.reload(cf_mod)
    importlib.reload(vks_mod)

    for app_name in (
        "gitea",
        "gitea-runner",
        "cloudflared",
        "vaultwarden-k8s-sync",
    ):
        cls = app_by_name(app_name)
        assert cls is not None, f"app {app_name!r} is not registered"
        assert issubclass(cls, BaseApp), (
            f"app {app_name!r} ({cls.__name__}) does not "
            f"subclass BaseApp"
        )


def test_baseapp_namespace_and_release_default_to_name() -> None:
    """`namespace` and `release` are convenience properties.
    Apps that don't override them inherit `self.name`.

    This pins the `BaseApp` API: apps don't need to redeclare
    `namespace = "..."` when it equals their `name`."""

    class SameNameApp(BaseApp):
        name = "same"

        def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppPlanResult(app_name=self.name)

        def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppApplyResult(
                app_name=self.name,
                namespace=self.namespace,
                release=self.release,
                chart_version="",
                image_version="",
            )

        def destroy(self, ctx, catalog) -> None:
            pass

        def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppStatus(
                app_name=self.name,
                namespace=self.namespace,
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
            )

    app = SameNameApp()
    assert app.namespace == "same"
    assert app.release == "same"


def test_register_rejects_dataclass_subclass() -> None:
    """WP0 forbids `@dataclass` on `BaseApp` subclasses
    (apps are behaviour, not data). The check fires at
    `@register` time, after `__init_subclass__` has run
    and the dataclass-generated `__init__` is in place.

    A `@dataclass`-decorated `BaseApp` subclass that
    happens to declare all four abstract methods can be
    instantiated (dataclass's `__init__` makes that
    possible), but `@register` rejects it."""

    @dataclass
    class DataclassApp(BaseApp):
        name: str = "dataclass-app"

        def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppPlanResult(app_name=self.name)

        def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppApplyResult(
                app_name=self.name,
                namespace="",
                release="",
                chart_version="",
                image_version="",
            )

        def destroy(self, ctx, catalog) -> None:
            pass

        def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
            return AppStatus(
                app_name=self.name,
                namespace="",
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
            )

    with pytest.raises(TypeError, match="must not be decorated with @dataclass"):
        register(DataclassApp)
