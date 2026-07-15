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
import os
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
    #
    # WP13 — `namespace` / `release` resolve in this
    # order at access time:
    #
    #   1. class-level `namespace = "..."` (the WP13
    #      explicit form)
    #   2. instance-level override (`_namespace_override`,
    #      kept for backward compat with the pre-WP13
    #      setter API)
    #   3. fallback to `self.name`
    #
    # Reading the class attribute first means the
    # WP13 class-attribute form takes precedence over
    # any leftover instance override; the setter
    # continues to work for tests and runtime tweaks.

    def _resolve_app_attr(
        self, class_attr_name: str, instance_attr_name: str
    ) -> str:
        """Look up an app-identity attribute on the
        class first, the instance override second,
        and fall back to `self.name`."""
        cls_value = getattr(type(self), class_attr_name, None)
        if isinstance(cls_value, str) and cls_value:
            return cls_value
        instance_override = getattr(self, instance_attr_name, None)
        if isinstance(instance_override, str) and instance_override:
            return instance_override
        return self.name

    @property
    def namespace(self) -> str:
        """The k8s namespace for this app.

        Defaults to `self.name`. Apps whose namespace
        differs from their registered name override
        `namespace` as a class attribute:

            class GiteaApp(BaseApp):
                namespace = "gitea"

        The setter (`app.namespace = "foo"`) continues
        to work for tests and ad-hoc overrides.
        """
        return self._resolve_app_attr("namespace", "_namespace_override")

    @namespace.setter
    def namespace(self, value: str) -> None:
        object.__setattr__(self, "_namespace_override", value)

    @property
    def release(self) -> str:
        """The helm release name for this app.

        Defaults to `self.name`. Apps whose helm release
        name differs from their registered name override
        `release` as a class attribute (see `namespace`).
        """
        return self._resolve_app_attr("release", "_release_override")

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

    # ----- .env parsing (WP11) -----

    @staticmethod
    def _parse_dotenv(text: str) -> dict[str, str]:
        """Best-effort `.env` parser used by every app
        that reads operator secrets.

        WP11 lifts the three duplicate parsers
        (`cloudflared._parse_dotenv`,
        `cloudflared._load_dotenv`,
        `vaultwarden_k8s_sync._load_dotenv`) onto
        `BaseApp`. Behaviour is the union — the most
        permissive of the three pre-WP11 shapes:

          * blank lines and `#` comments are dropped
          * single- and double-quoted values have the
            surrounding quotes stripped; a `#` *inside*
            a quoted value is part of the value
          * `export FOO=bar` parses as `FOO=bar`
            (POSIX shell convention; some operators
            source these files in their shell profile)
          * bare `KEY=value` is the dominant case
          * lines without `=` are dropped silently
          * unknown keys land in the dict verbatim —
            the calling app's `_require_env` raises if
            the canonical key is missing

        No `${VAR}` expansion (the codebase has never
        used it; introducing it here would change
        observable behaviour for keys that contain
        literal `$`).

        Returns `dict[str, str]`. Empty dict for empty
        input.
        """
        result: dict[str, str] = {}
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Strip an optional `export ` prefix.
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].lstrip()
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            result[key] = value
        return result

    @staticmethod
    def _load_dotenv(repo_root: Path) -> dict[str, str]:
        """Read `<repo_root>/.env` and parse it.

        WP11 — thin wrapper around `_parse_dotenv`.
        Missing file or unreadable file returns an
        empty dict (apps with no secrets — test paths,
        dry-runs against a fresh checkout — should
        *not* crash on the first read).

        Apps that own a canonical alias map
        (`vaultwarden_k8s_sync` keys multiple user-
        friendly spellings onto one canonical form)
        apply that mapping on top of the parser's
        raw output. WP11 keeps aliasing per-app; the
        parser is generic by design.
        """
        path = repo_root / ".env"
        if not path.exists():
            return {}
        try:
            return BaseApp._parse_dotenv(path.read_text(encoding="utf-8"))
        except OSError:
            return {}

    @staticmethod
    def _require_env(env: dict[str, str], key: str) -> str:
        """Return `env[key]` or raise a grep-able error.

        WP11 — three apps (`gitea`, `gitea-runner`'s
        sibling delegates, `cloudflared`) used to roll
        a private `_require_env` with slightly different
        error messages. Centralizing it on `BaseApp`
        keeps the operator-facing message the same.
        Defining it as a `@staticmethod` lets the
        pre-WP11 static call sites
        (`CloudflareApp._require_env({}, ...)` in
        tests, `VaultwardenK8sSyncApp._require_env(...)`
        in cross-app helpers) keep working unchanged.

        Apps that want the app-name in the error reach
        for `_require_env_for(self.name, env, key)` —
        the three production callers today all call
        `self._require_env(env, key)` so they get the
        static form. Adding the app-name to the error
        is a future tightening once every caller is
        migrated away from the static form.
        """
        value = env.get(key)
        if value is None or not value.strip():
            raise RuntimeError(
                f"missing required .env value {key!r}. "
                f"Set it in .env next to the proxmox-cicd "
                f"repo root or run setup."
            )
        return value.strip()

    # ----- kubeconfig resolution (WP6) -----

    def _kubectl(self, ctx: Any) -> Any:
        """Return a `KubectlRunner` bound to this app's
        per-cluster kubeconfig.

        WP6 — apps used to each carry a private
        `_kubectl` / `_kubeconfig` method that re-loaded
        the kubeconfig from the sibling `proxmox-k3s`
        repo, ran hand-rolled `subprocess` calls, or
        wrapped the same logic in slightly different
        ways. Centralizing the loader here means:

          * apps never import `kubeconfig_loader`
            (forward-compat: the orchestrator and CLI
            can later swap in an env-supplied kubeconfig
            without touching every app)
          * tests can stub the runner by setting
            `ctx.kubectl` and bypass disk I/O
          * a missing kubeconfig raises a single,
            consistent error message

        Resolution order:

          1. Return `ctx.kubectl` if the bootstrap path
             already attached a runner (test paths and
             the `CloudflareTunnel` bootstrap, where the
             runner comes from env vars not a file).
          2. Load
             `<proxmox_k3s_repo>/infra/clusters/<cluster>/kubeconfig.yaml`
             where `<cluster>` defaults to `cicd` and
             can be overridden via the
             `PROXMOX_CICD_CLUSTER` env var. The runner
             is built against this kubeconfig, cached
             back on the `Container` so subsequent calls
             are cheap (idempotent per-context), and
             returned.
          3. If the file does not exist, raise
             `RuntimeError` with a grep-able message
             pointing at `make apply` in `proxmox-k3s`
             as the fix.

        The `proxmox_k3s_repo` is the directory the
        orchestrator was launched from — i.e. the
        `proxmox-k3s` checkout on disk.
        """
        cached = getattr(ctx, "kubectl", None)
        if cached is not None:
            return cached

        # Lazy imports keep BaseApp importable in test
        # contexts that don't have the sibling repo
        # checked out (parametrize over `proxmox_k3s_repo`
        # can point at a tmp dir; the import graph stays
        # narrow).
        from ..container import Container  # noqa: F401
        from ..kubectl_runner import KubectlRunner
        from ..kubeconfig_loader import load

        repo = ctx.proxmox_k3s_repo
        cluster = os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")
        path = repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"
        if not path.exists():
            raise RuntimeError(
                f"kubeconfig not found at {path}. "
                f"Did you run `make apply` in proxmox-k3s?"
            )

        kubeconfig = load(path)
        runner = KubectlRunner(kubeconfig=kubeconfig, logger=ctx.logger)
        ctx.kubectl = runner
        return runner

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
