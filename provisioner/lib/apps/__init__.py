"""apps — AppSpec protocol + @register decorator.

This package contains the SOLID seam between the orchestrator
(everything app-agnostic) and the per-app implementations
(everything app-specific). Adding a new app is a one-file
change: create apps/<name>.py with an AppSpec subclass
decorated by `@register`. The orchestrator discovers it via
import-time side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from collections.abc import Callable

from ..container import Container
from ..log import StructuredLogger


@dataclass
class AppPlanResult:
    """What `AppSpec.plan()` returns: a diff summary suitable
    for `cicdctl plan cicd` to print to the operator.
    """

    app_name: str
    would_install: list[str] = field(default_factory=list)
    would_apply: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AppApplyResult:
    """What `AppSpec.apply()` returns: the audit-log-friendly
    summary of what was actually done.
    """

    app_name: str
    namespace: str
    release: str
    chart_version: str
    image_version: str
    ingress_host: str | None = None


@dataclass
class AppStatus:
    """What `AppSpec.status()` returns: the live state for
    `cicdctl status cicd`.
    """

    app_name: str
    namespace: str
    release_present: bool
    chart_version: str | None
    image_version: str | None
    ingress_host: str | None
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class AppSpec(Protocol):
    """Every app in the catalog implements this protocol.

    The orchestrator only knows about the 4 methods + the
    `name` field; the actual values, manifests, and probes
    are private to each subclass.

    SOLID notes:
      S — one subclass per app, one file per subclass.
      O — adding a new app = new subclass + @register, no
          changes to the orchestrator.
      L — every subclass honors the same 4-method contract;
          the orchestrator can swap any app for any other.
      I — the protocol exposes only what the orchestrator
          needs; helpers stay private.
      D — apps take a `Container`, not concrete runners.
    """

    name: str

    def plan(self, ctx: Container, catalog: dict[str, Any]) -> AppPlanResult: ...

    def apply(self, ctx: Container, catalog: dict[str, Any]) -> AppApplyResult: ...

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None: ...

    def status(self, ctx: Container, catalog: dict[str, Any]) -> AppStatus: ...


# ----- registry -----


_REGISTRY: dict[str, type[AppSpec]] = {}


def register(cls: type[AppSpec]) -> type[AppSpec]:
    """Decorator: register `cls` in the global app registry.

    Apps import this from `provisioner.lib.apps` and decorate
    their AppSpec subclass. The orchestrator pulls them back
    out via `all_apps()`.

    Idempotent on the same class object: a re-decorated
    `GiteaApp` doesn't re-register, even though Python
    creates a fresh class object on module reload. We detect
    the "same logical app" by `cls.__module__ + cls.__qualname__`
    so test re-imports are safe.
    """
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


def all_apps() -> tuple[type[AppSpec], ...]:
    """Return every registered AppSpec subclass, in registration order."""
    return tuple(_REGISTRY.values())


def app_by_name(name: str) -> type[AppSpec] | None:
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
    "AppSpec",
    "AppStatus",
    "all_apps",
    "app_by_name",
    "register",
    "reset_registry",
]
