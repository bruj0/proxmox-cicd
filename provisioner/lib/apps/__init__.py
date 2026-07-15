"""apps — BaseApp ABC + @register decorator.

This package contains the SOLID seam between the orchestrator
(everything app-agnostic) and the per-app implementations
(everything app-specific). Adding a new app is a one-file
change: create apps/<name>.py with a BaseApp subclass
decorated by `@register`. The orchestrator discovers it via
import-time side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from ..log import StructuredLogger
from .base import BaseApp


@dataclass
class AppPlanResult:
    """What `BaseApp.plan()` returns: a diff summary suitable
    for `cicdctl plan cicd` to print to the operator.
    """

    app_name: str
    would_install: list[str] = field(default_factory=list)
    would_apply: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AppApplyResult:
    """What `BaseApp.apply()` returns: the audit-log-friendly
    summary of what was actually done.

    `next_step` is the operator-facing follow-up message:
    surfaced verbatim by the orchestrator right after the
    install line. Apps that complete fully (no manual
    intervention) leave it as None. Apps that need a
    follow-up action from the operator (e.g. the Gitea
    runner can't register until the user pastes a token)
    populate it with a one-sentence call to action.
    """

    app_name: str
    namespace: str
    release: str
    chart_version: str
    image_version: str
    ingress_host: str | None = None
    next_step: str | None = None


@dataclass
class AppStatus:
    """What `BaseApp.status()` returns: the live state for
    `cicdctl status cicd`.
    """

    app_name: str
    namespace: str
    release_present: bool
    chart_version: str | None
    image_version: str | None
    ingress_host: str | None
    notes: list[str] = field(default_factory=list)


# ----- Backward-compat alias -----
#
# WP15 — `AppSpec` was the legacy Protocol that WP0
# replaced with `BaseApp`. Tests had `isinstance(x, AppSpec)`
# runtime checks; third-party type-narrowing used
# `Protocol` semantics. Keep `AppSpec` as a bare alias
# for `BaseApp` so:
#
#   * `isinstance(x, AppSpec)` keeps working (now it's
#     a real ABC subclass check — strictly stronger
#     than the old runtime Protocol check).
#   * `from provisioner.lib.apps import AppSpec` keeps
#     working for code written against the WP0-pre
#     surface.
#
# A ruff `forbidden-name` rule + a static guard
# (`tests/test_apps_no_appspec_refs.py`) block new
# `AppSpec` references in app code. The alias below is
# the one allowed site.
AppSpec = BaseApp


# ----- registry -----


_REGISTRY: dict[str, type[BaseApp]] = {}


def register(cls: type[BaseApp]) -> type[BaseApp]:
    """Decorator: register `cls` in the global app registry.

    Apps import this from `provisioner.lib.apps` and decorate
    their `BaseApp` subclass. The orchestrator pulls them back
    out via `all_apps()`.

    WP0 invariants enforced here:

      1. `cls` must be a subclass of `BaseApp` (rejects the
         old `@dataclass`-with-no-base-shape that pre-WP0
         code used).
      2. `cls` must NOT be decorated with `@dataclass`
         (apps are behaviour with stable identity, not data;
         a dataclass-generated `__init__` shadows the
         no-arg instantiation contract).
      3. `name` must be a non-empty string.

    Idempotent on the same class object: a re-decorated
    `GiteaApp` doesn't re-register, even though Python
    creates a fresh class object on module reload. We detect
    the "same logical app" by `cls.__module__ + cls.__qualname__`
    so test re-imports are safe.
    """
    if not isinstance(cls, type) or not issubclass(cls, BaseApp):
        raise TypeError(
            f"{cls.__name__ if isinstance(cls, type) else cls!r} "
            f"must subclass BaseApp to be @register'ed."
        )
    if hasattr(cls, "__dataclass_fields__"):
        raise TypeError(
            f"{cls.__name__} must not be decorated with @dataclass; "
            f"apps are behaviour with stable identity, not data. "
            f"Inherit BaseApp and declare `name` as a class attribute."
        )
    name = getattr(cls, "name", None)
    if not name:
        raise TypeError(
            f"{cls.__name__} must define a non-empty `name` class attr "
            f"to be @register'ed."
        )
    if name in _REGISTRY:
        existing = _REGISTRY[name]
        # Same module+qualname? Treat as the same logical app
        # (the test reloaded the module; the class object is
        # different but the source is identical).
        if (existing.__module__, existing.__qualname__) != (
            cls.__module__,
            cls.__qualname__,
        ):
            raise ValueError(
                f"app name '{name}' already registered to {existing.__name__}"
            )
    _REGISTRY[name] = cls
    return cls


def all_apps() -> tuple[type[BaseApp], ...]:
    """Return every registered BaseApp subclass, in registration order."""
    return tuple(_REGISTRY.values())


def app_by_name(name: str) -> type[BaseApp] | None:
    """Look up a single app class by its registered name."""
    return _REGISTRY.get(name)


def reset_registry() -> None:
    """Clear the registry. Used by tests to isolate side effects."""
    _REGISTRY.clear()


def _make_logger_sink(path: Path, step: str, message: str) -> Callable[..., None]:
    """Build a `logger.info`-shaped callable for the apps."""
    logger: StructuredLogger = StructuredLogger(audit_path=path)
    return lambda **kw: logger.info(step, message, **kw)


__all__ = [
    "AppApplyResult",
    "AppPlanResult",
    "AppSpec",  # legacy: re-exported as an alias for BaseApp
    "AppStatus",
    "BaseApp",
    "all_apps",
    "app_by_name",
    "register",
    "reset_registry",
]
