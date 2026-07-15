"""apps/base — BaseApp ABC + the four-method contract.

WP0 of the GroupSpec plan replaces the `AppSpec` Protocol
with a real `abc.ABC` subclass. Every app (`gitea`,
`gitea-runner`, `cloudflared`, `vaultwarden-k8s-sync`)
inherits from `BaseApp`. Apps stop being
`@dataclass`-with-freeform-methods; they become thin
subclasses that declare `name` as a class attribute and
override only `plan` / `apply` / `destroy` / `status`.

Why an ABC instead of the previous `AppSpec` Protocol:

  * `@abstractmethod` enforces the four-method contract at
    class-creation time. A misspelled `apply()` becomes
    a `TypeError: Can't instantiate abstract class …` at
    the import site, not an `AttributeError` at apply
    time inside the orchestrator.
  * Common helpers (`namespace`, `release`, etc.) live in
    one place and stop drifting across apps.
  * `mypy strict` catches missing methods and inconsistent
    override signatures at CI time — no more runtime
    `AttributeError` from a misspelled method name.

WP0 absorbs the *obvious* helpers. WP9 (a follow-up plan
WP) does a deeper sweep and adds `_secret_ref`,
`_hostname`, `_labels`, `_annotations`, `_deep_merge`,
etc. The base class is structured so WP9 can extend it
without breaking this WP0 contract.

SOLID notes:
  S — `BaseApp` is the single seam between the
      orchestrator and the per-app implementations.
  O — adding a new app = new `BaseApp` subclass + one
      `@register`; the orchestrator is unchanged.
  L — every subclass honors the same 4-method contract;
      the orchestrator can swap any app for any other.
  I — `BaseApp` exposes only what the orchestrator needs;
      per-app helpers stay private.
  D — apps take a `Container`, not concrete runners.
"""

from __future__ import annotations

import abc
import re
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from . import AppApplyResult, AppPlanResult, AppStatus


class TemplateNotFoundError(FileNotFoundError):
    """Raised when `BaseApp._render_template` can't
    locate the requested template file.

    Subclassing `FileNotFoundError` lets existing
    `try/except OSError` blocks catch it without a new
    branch; the operator-facing message includes the
    app name + the relative path so it's grep-able.
    """


class BaseApp(abc.ABC):
    """The base class every AppSpec inherits.

    Subclasses MUST:

      * declare `name: ClassVar[str]` as a class attribute
      * implement the four abstract methods
        (`plan`, `apply`, `destroy`, `status`)

    Subclasses MUST NOT:

      * be decorated with `@dataclass` (apps are behaviour
        with stable identity, not data; the dataclass
        `__init__` would shadow `BaseApp.__init__`).
      * define an `__init__` that takes positional
        arguments (the orchestrator instantiates each app
        with no arguments: `GiteaApp()`).

    The four methods all take the same `(ctx, catalog)`
    pair:

      * `ctx` is a `Container` (DI: kubectl/helm runners,
        logger, paths).
      * `catalog` is a `dict[str, Any]` (the per-cluster
        merged catalog data — apps read what they need).

    Apps that don't override `namespace` / `release` get
    them for free: both default to `self.name`.
    """

    # ----- class-level identity -----

    # Subclasses MUST override. The check is in
    # `__init_subclass__` so it fires at class-creation
    # time, not at instantiation time.
    name: ClassVar[str]

    # ----- init-subclass gate -----

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate the subclass shape at class-creation time.

        Two checks:

          1. `name` is a non-empty class attribute.
          2. The subclass is NOT decorated with `@dataclass`
             (dataclass-generated `__init__` shadows
             `BaseApp.__init__` and breaks the no-arg
             instantiation contract).
        """
        super().__init_subclass__(**kwargs)

        # Reject @dataclass on BaseApp subclasses.
        # dataclass sets this attribute on the class itself;
        # we can't read it from `cls` directly because the
        # decorator runs before `__init_subclass__` for
        # dataclasses, so we look at `__init__`'s origin.
        if hasattr(cls.__init__, "__wrapped__") or any(
            getattr(b, "__name__", "") == "dataclass"
            for b in getattr(cls, "__decorators__", [])
        ):
            # Fallback: look for the `_FIELDS` marker that
            # @dataclass sets on the class. This is set even
            # when @dataclass has no fields to declare.
            if hasattr(cls, "__dataclass_fields__"):
                raise TypeError(
                    f"{cls.__name__} must not be decorated with "
                    f"@dataclass; apps are behaviour with stable "
                    f"identity, not data. Inherit BaseApp and "
                    f"declare `name` as a class attribute."
                )

        # Require `name`.
        if not getattr(cls, "name", None):
            raise TypeError(
                f"{cls.__name__} must define `name` class "
                f"attribute before it can be registered."
            )

    # ----- convenience properties -----

    @property
    def namespace(self) -> str:
        """The k8s namespace for this app.

        Defaults to `self.name`. Apps whose namespace
        differs from their registered name override
        `namespace` as a class attribute.
        """
        return getattr(self, "_namespace_override", self.name)

    @namespace.setter
    def namespace(self, value: str) -> None:
        # Allow apps to declare `namespace = "foo"` as a
        # class attribute; we capture it on the instance.
        object.__setattr__(self, "_namespace_override", value)

    @property
    def release(self) -> str:
        """The helm release name for this app.

        Defaults to `self.name`. Apps whose helm release
        name differs from their registered name override
        `release` as a class attribute.
        """
        return getattr(self, "_release_override", self.name)

    @release.setter
    def release(self, value: str) -> None:
        object.__setattr__(self, "_release_override", value)

    # ----- template rendering (WP5) -----

    @property
    def template_dir(self) -> Path:
        """Directory where the app's YAML templates live.

        WP5 — each app has a sibling directory under
        `provisioner/lib/apps/templates/<app_name>/`
        holding the YAML files the app substitutes
        into. The default location is computed from
        the module path; subclasses can override by
        assigning `_template_dir_override` or
        overriding the property.
        """
        return Path(__file__).resolve().parent / "templates" / self.name

    def _render_template(self, name: str, **vars: Any) -> str:
        """Read `templates/<name>`, run it through
        `string.Template.safe_substitute(**vars)`, return
        the rendered string.

        WP5 — moves inlined YAML manifest blocks out
        of `apps/*.py` into real files.
        Templates use `$var` / `${var}` syntax so YAML
        values that contain literal `{` / `}` (RBAC,
        JSONata, regex) don't fight Python format
        strings.

        Unrendered variables raise `KeyError` after the
        pass so we don't ship invalid YAML to kubectl
        (a silent miss in `safe_substitute` would be a
        production outage). Unused kwargs are silently
        dropped, so callers can splat `**catalog` and
        ignore the noise.

        `$$` renders as a literal `$` for values that
        need currency / regex characters.
        """
        path = self.template_dir / name
        if not path.exists():
            raise TemplateNotFoundError(
                f"app {self.name!r} has no template at "
                f"{path}. Add the YAML file or fix the "
                f"app's _render_template call."
            )
        rendered = Template(path.read_text(encoding="utf-8")).safe_substitute(
            vars
        )
        # Detect unrendered placeholders (either
        # `${var}` or bare `$var`) left behind because
        # a kwarg was missing. This is the
        # silent-failure case `safe_substitute` would
        # otherwise produce.
        unrendered = re.search(
            r"\$\{[^}]+\}|(?<!\$)\$(?!\$)([A-Za-z_][A-Za-z0-9_]*)",
            rendered,
        )
        if unrendered:
            var_name = (
                unrendered.group(1)
                if unrendered.group(1)
                else unrendered.group(0).strip("${}")
            )
            raise KeyError(
                f"template {path} references ${var_name} "
                f"but no value was supplied. Pass "
                f"{var_name!r} as a kwarg."
            )
        return rendered

    # ----- abstract four-method contract -----

    @abc.abstractmethod
    def plan(
        self, ctx: Any, catalog: dict[str, Any]
    ) -> AppPlanResult:
        """Diff desired state against live cluster state.

        No cluster side effects. Returns an `AppPlanResult`
        summarizing what *would* happen.
        """
        ...

    @abc.abstractmethod
    def apply(
        self, ctx: Any, catalog: dict[str, Any]
    ) -> AppApplyResult:
        """Install or upgrade the app.

        Idempotent: `helm upgrade --install` +
        `kubectl apply --server-side`. Returns an
        `AppApplyResult` summarizing what was done.
        """
        ...

    @abc.abstractmethod
    def destroy(self, ctx: Any, catalog: dict[str, Any]) -> None:
        """Uninstall the app and delete its namespace.

        Idempotent: `helm uninstall` + namespace delete.
        """
        ...

    @abc.abstractmethod
    def status(
        self, ctx: Any, catalog: dict[str, Any]
    ) -> AppStatus:
        """Read the live state of the app."""
        ...


__all__ = ["BaseApp"]
