"""catalog — load + validate infra/clusters/<name>/catalog.yaml.

The catalog is the operator-edited file that says which apps
are enabled for THIS cluster, plus per-cluster overrides
(ingress hostname, Bitwarden org ID, etc.).

Shape (YAML, parsed narrowly with stdlib regex):

  cluster_name: cicd
  ingress:
    base_domain: example.net      # gitea becomes gitea.example.net
  vaultwarden:
    server_url: https://bitwarden.example.net
  apps:
    gitea:
      enabled: true
    gitea-runner:
      enabled: true
    vaultwarden-k8s-sync:
      enabled: true

Validation rules:
  - At least one app is enabled.
  - Every enabled app name must exist in the registry.
  - `cluster_name` must match the CLI argument.
  - `ingress.base_domain` must be a valid DNS label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path



_DNS_LABEL_RE = re.compile(
    r"^(?:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


class CatalogError(ValueError):
    """Raised when catalog.yaml is malformed or invalid."""


@dataclass
class AppConfig:
    enabled: bool = False
    extra: dict[str, str] = field(default_factory=dict)
    # WP1 added: per-app values overlay. After the merge
    # against the shipped catalog's `default_values`,
    # this holds the final per-app config dict. Apps
    # read it via `catalog.apps[app_name].values`.
    values: dict[str, object] = field(default_factory=dict)


@dataclass
class Catalog:
    cluster_name: str
    apps: dict[str, AppConfig]
    ingress_base_domain: str
    vaultwarden_server_url: str = ""
    # Optional knobs the orchestrator reads when it talks
    # to Vaultwarden. `skip_admin_seed` exists so the
    # gitea app's apply can run in unit-test mode
    # without a live Vaultwarden (cluster Secret is
    # still written; only the Secure Note push is
    # suppressed). `email` overrides the canonical
    # operator account the Vaultwarden client logs in
    # as.
    vaultwarden_email: str = ""
    vaultwarden_skip_admin_seed: bool = False
    vaultwarden_skip_runner_seed: bool = False
    source_path: Path | None = None

    def enabled_app_names(self) -> list[str]:
        return sorted(n for n, c in self.apps.items() if c.enabled)

    def as_dict(self) -> dict[str, object]:
        """Render to a dict the apps can read directly.
        Apps expect `catalog["ingress"]["base_domain"]` etc.
        """
        return {
            "cluster_name": self.cluster_name,
            "ingress": {"base_domain": self.ingress_base_domain},
            "vaultwarden": {
                "server_url": self.vaultwarden_server_url,
                "email": self.vaultwarden_email,
                "skip_admin_seed": self.vaultwarden_skip_admin_seed,
                "skip_runner_seed": self.vaultwarden_skip_runner_seed,
            },
            "apps": {
                name: {"enabled": cfg.enabled, **cfg.extra}
                for name, cfg in self.apps.items()
            },
        }

    @classmethod
    def from_shipped_and_cluster(
        cls,
        shipped: ShippedCatalog,
        cluster: Catalog,
    ) -> Catalog:
        """Build the merged per-cluster catalog from the
        shipped catalog (single source of truth) plus the
        operator-edited cluster override layer.

        Merge rule (§5.2):

        - A cluster `apps:<name>.enabled: <bool>` flips
          the merged catalog's `enabled` flag. Absent ->
          treated as `False`. Apps not listed in the
          cluster catalog are absent from the merged
          catalog's `enabled_app_names()`.
        - A cluster `apps:<name>.values:` mapping is
          deep-merged on top of the shipped
          `default_values:` for the same app.
        - A cluster reference to an app not in the
          shipped catalog raises `CatalogError` listing
          the unknown name(s). The shipped catalog is
          the only place apps can be defined.

        The returned object preserves all the per-cluster
        fields (`cluster_name`, `ingress_base_domain`,
        `vaultwarden_*`). It is the orchestrator's
        working catalog going forward; the standalone
        `Catalog` from `load_catalog()` is only an
        intermediate.
        """
        merged_apps: dict[str, AppConfig] = {}
        unknown: list[str] = []

        # Iterate the **shipped** catalog: every shipped
        # app gets a slot in the merged catalog, with
        # `enabled` defaulting to `False`. Apps not
        # mentioned in the cluster catalog stay disabled.
        for app_name, shipped_app in shipped.apps.items():
            cluster_cfg = cluster.apps.get(app_name)
            if cluster_cfg is None:
                # Not in cluster catalog -> disabled, no
                # overlay. Keep the slot so the merged
                # catalog reflects the full shipped app
                # set.
                merged_apps[app_name] = AppConfig(
                    enabled=False,
                    extra={},
                    values=dict(shipped_app.default_values),
                )
                continue

            # Cluster opted in (or explicitly disabled).
            # Either way, we validate the app is known —
            # because we're iterating shipped.apps, it
            # always is, but the cluster-only entries
            # surface in the next loop.
            merged_values = _deep_merge(
                dict(shipped_app.default_values),
                dict(cluster_cfg.values),
            )
            merged_apps[app_name] = AppConfig(
                enabled=cluster_cfg.enabled,
                extra=dict(cluster_cfg.extra),
                values=merged_values,
            )

        # Detect cluster apps not in shipped.
        for app_name in cluster.apps:
            if app_name not in shipped.apps:
                unknown.append(app_name)
        if unknown:
            raise CatalogError(
                f"per-cluster catalog references unknown "
                f"app(s) {sorted(unknown)}; the shipped "
                f"catalog at {shipped.source_path} (version "
                f"{shipped.version}) is the single source of "
                f"truth. Either remove the unknown entries "
                f"from the per-cluster catalog or extend "
                f"the shipped catalog."
            )

        return cls(
            cluster_name=cluster.cluster_name,
            apps=merged_apps,
            ingress_base_domain=cluster.ingress_base_domain,
            vaultwarden_server_url=cluster.vaultwarden_server_url,
            vaultwarden_email=cluster.vaultwarden_email,
            vaultwarden_skip_admin_seed=(
                cluster.vaultwarden_skip_admin_seed
            ),
            vaultwarden_skip_runner_seed=(
                cluster.vaultwarden_skip_runner_seed
            ),
            source_path=cluster.source_path,
        )


# ----- shipped catalog (WP1) -----


@dataclass(frozen=True)
class ShippedApp:
    """One app the provisioner knows how to install.

    Defined in `provisioner/lib/catalog/shipped.yaml`
    (committed to the codebase, code-reviewed). A
    per-cluster catalog.yaml can enable/disable a
    `ShippedApp` and override values; it cannot
    introduce an app the shipped catalog doesn't know
    about. See §5.2.
    """

    name: str
    description: str
    namespace: str
    release: str
    chart: str
    chart_version: str
    image_version: str
    # Ship-time defaults for the per-app `values:` overlay.
    # Populated from `default_values:` in `shipped.yaml`;
    # missing entries default to an empty dict.
    default_values: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ShippedCatalog:
    """The codebase-shipped app catalog.

    Loaded once per orchestrator startup from
    `provisioner/lib/catalog/shipped.yaml`. The merged
    per-cluster catalog (see `Catalog.from_shipped_and_cluster`)
    must only reference apps listed here.
    """

    version: str
    apps: dict[str, ShippedApp]
    source_path: Path | None = None


def load_shipped_catalog(path: Path) -> ShippedCatalog:
    """Load the codebase-shipped catalog from `shipped.yaml`.

    The shipped catalog is the version contract: it lists
    every app this version of `proxmox-cicd` knows how
    to install, with namespace / release / chart / version
    pins. Editing it is a code change. The per-cluster
    `infra/clusters/<name>/catalog.yaml` becomes a thin
    enablement + value-override layer on top.

    The shipped YAML has the same narrow-YAML parser
    constraints as the per-cluster catalog: top-level
    `version` (string), `apps` (mapping of `app_name` →
    `ShippedApp` mapping). See `shipped.yaml` for the
    schema.
    """
    if not path.exists():
        raise CatalogError(
            f"shipped catalog not found at {path}. "
            f"WP1 requires provisioner/lib/catalog/shipped.yaml "
            f"to be committed to the codebase."
        )
    text = path.read_text(encoding="utf-8")
    parsed = _parse_yaml_simple(text)

    version = parsed.get("version", "")
    if not isinstance(version, str) or not version:
        raise CatalogError(
            f"shipped catalog at {path} is missing top-level "
            f"`version:` (a semver string)"
        )

    apps_raw = parsed.get("apps", {})
    if not isinstance(apps_raw, dict) or not apps_raw:
        raise CatalogError(
            f"shipped catalog at {path} has empty or missing "
            f"`apps:` mapping"
        )

    apps: dict[str, ShippedApp] = {}
    for name, cfg in apps_raw.items():
        if not isinstance(cfg, dict):
            raise CatalogError(
                f"shipped app {name!r} in {path} must be a mapping"
            )
        # Required keys.
        missing = [
            k
            for k in (
                "description",
                "namespace",
                "release",
                "chart",
                "chart_version",
                "image_version",
            )
            if not isinstance(cfg.get(k), str)
            or not str(cfg.get(k, "")).strip()
        ]
        if missing:
            raise CatalogError(
                f"shipped app {name!r} in {path} is missing "
                f"required keys: {missing}"
            )
        # Optional `default_values:` mapping (added in WP10,
        # but the loader accepts it from WP1 so the YAML
        # can carry it without breaking).
        default_values_raw = cfg.get("default_values", {}) or {}
        if not isinstance(default_values_raw, dict):
            raise CatalogError(
                f"shipped app {name!r}.default_values in {path} "
                f"must be a mapping"
            )
        apps[str(name)] = ShippedApp(
            name=str(name),
            description=str(cfg["description"]),
            namespace=str(cfg["namespace"]),
            release=str(cfg["release"]),
            chart=str(cfg["chart"]),
            chart_version=str(cfg["chart_version"]),
            image_version=str(cfg["image_version"]),
            default_values=dict(default_values_raw),
        )

    return ShippedCatalog(
        version=version,
        apps=apps,
        source_path=path,
    )


def _deep_merge(
    base: dict[str, object],
    overlay: dict[str, object],
) -> dict[str, object]:
    """Recursively merge `overlay` into `base`.

    WP9 — thin wrapper around `BaseApp._deep_merge`
    so the canonical merge logic lives in one place
    (the apps are the largest consumers; the catalog
    loader was the original implementation). New
    callers should reach for `BaseApp._deep_merge`
    directly; this shim keeps the WP1-era call sites
    in catalog.py (`_deep_merge(shipped_default,
    cluster_values)`) working untouched.

    Behaviour is unchanged: pure-function, recursive
    merge of nested dicts, shallow replace for
    everything else.
    """
    # Lazy import: catalog.py is imported by
    # `apps/__init__.py`'s registration side effects;
    # importing apps.base from the top of catalog.py
    # would invert the package dependency direction
    # for no real benefit.
    from .apps.base import BaseApp

    return BaseApp._deep_merge(base, overlay)


def _parse_scalar(value: str) -> object:
    """Coerce a YAML scalar string into a Python value.

    Only handles the subset of scalars that proxmox-cicd's
    catalog.yaml actually uses: bool, int, string. No
    floats, no null.
    """
    s = value.strip()
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    return s


def _parse_yaml_simple(text: str) -> dict[str, object]:
    """Very narrow YAML subset parser. Supports:
      - top-level keys with string/int/bool values
      - 2-level nesting: `parent:\n  child: value`
      - list items under a key: `key:\n  - item\n  - item`

    Anything fancier -> raise CatalogError.
    """
    lines = text.splitlines()
    root: dict[str, object] = {}
    stack: list[tuple[int, dict[str, object]]] = [(-1, root)]
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # Compute indent depth.
        indent = len(line) - len(line.lstrip())
        # Pop the stack until we're at the right depth.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        # List item?
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            value = _parse_scalar(item_text)
            # The parent for list items must be a list.
            if not isinstance(parent, list):
                raise CatalogError(
                    f"list item under non-list key: {line!r}"
                )
            parent.append(value)
        else:
            # key: value or key: (nested)
            if ":" not in stripped:
                raise CatalogError(
                    f"expected `key: value`, got: {line!r}"
                )
            key, _, value_text = stripped.partition(":")
            key = key.strip()
            value_text = value_text.strip()
            if value_text == "":
                # Nested mapping or list.
                # Look ahead: if next non-empty line starts with
                # `  - `, treat as list; else as mapping.
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    next_indent = (
                        len(lines[j]) - len(lines[j].lstrip())
                    )
                    if next_indent > indent and lines[j].lstrip().startswith(
                        "- "
                    ):
                        new: object = []
                    else:
                        new = {}
                else:
                    new = {}
                parent[key] = new
                stack.append((indent, new))  # type: ignore[arg-type]
            else:
                parent[key] = _parse_scalar(value_text)
        i += 1
    return root


def load_catalog(path: Path, cluster_name: str) -> Catalog:
    """Read `path`, validate, return a Catalog dataclass."""
    if not path.exists():
        raise CatalogError(
            f"catalog not found at {path}. "
            f"Create infra/clusters/{cluster_name}/catalog.yaml."
        )

    text = path.read_text(encoding="utf-8")
    parsed = _parse_yaml_simple(text)

    catalog_cluster = parsed.get("cluster_name")
    if not isinstance(catalog_cluster, str):
        raise CatalogError(
            f"catalog at {path} is missing top-level `cluster_name:`"
        )
    if catalog_cluster != cluster_name:
        raise CatalogError(
            f"catalog's cluster_name ({catalog_cluster!r}) does not "
            f"match the CLI argument ({cluster_name!r})"
        )

    # ingress.base_domain
    ingress = parsed.get("ingress", {})
    base_domain = ""
    if isinstance(ingress, dict):
        raw_base = ingress.get("base_domain", "")
        if isinstance(raw_base, str):
            base_domain = raw_base
    if not base_domain:
        raise CatalogError(
            f"catalog at {path} is missing `ingress.base_domain:`"
        )
    if not _DNS_LABEL_RE.match(base_domain):
        raise CatalogError(
            f"catalog's ingress.base_domain ({base_domain!r}) is not "
            f"a valid DNS label"
        )

    # vaultwarden.* (optional but recorded)
    vw = parsed.get("vaultwarden", {})
    vw_url = ""
    vw_email = ""
    vw_skip_admin_seed = False
    vw_skip_runner_seed = False
    if isinstance(vw, dict):
        v = vw.get("server_url", "")
        if isinstance(v, str):
            vw_url = v
        e = vw.get("email", "")
        if isinstance(e, str):
            vw_email = e
        s = vw.get("skip_admin_seed", False)
        if isinstance(s, bool):
            vw_skip_admin_seed = s
        rs = vw.get("skip_runner_seed", False)
        if isinstance(rs, bool):
            vw_skip_runner_seed = rs

    # apps.*
    apps_raw = parsed.get("apps", {})
    if not isinstance(apps_raw, dict):
        raise CatalogError(
            f"catalog at {path} is missing `apps:` mapping"
        )

    apps: dict[str, AppConfig] = {}
    for name, cfg in apps_raw.items():
        if not isinstance(cfg, dict):
            raise CatalogError(
                f"apps.{name} in {path} must be a mapping"
            )
        enabled_raw = cfg.get("enabled", False)
        if not isinstance(enabled_raw, bool):
            raise CatalogError(
                f"apps.{name}.enabled in {path} must be a boolean"
            )
        # `values:` is the per-cluster overlay used by
        # the WP1 merge against the shipped catalog's
        # `default_values:`. It must be a mapping; if
        # absent, the merged values come from the shipped
        # defaults alone.
        values_raw = cfg.get("values", {}) or {}
        if not isinstance(values_raw, dict):
            raise CatalogError(
                f"apps.{name}.values in {path} must be a "
                f"mapping; got {type(values_raw).__name__}"
            )
        values: dict[str, object] = {
            str(k): v for k, v in values_raw.items()
        }
        extra: dict[str, str] = {
            str(k): str(v)
            for k, v in cfg.items()
            if k not in ("enabled", "values")
        }
        apps[str(name)] = AppConfig(
            enabled=enabled_raw, extra=extra, values=values
        )

    enabled_names = [n for n, c in apps.items() if c.enabled]
    if not enabled_names:
        raise CatalogError(
            f"catalog at {path} enables zero apps; nothing to apply."
        )

    # Every enabled app must exist in the registry.
    # We don't import apps here (that would force-import every
    # app on every catalog load); the caller (orchestrator)
    # imports them once at startup.
    return Catalog(
        cluster_name=cluster_name,
        apps=apps,
        ingress_base_domain=base_domain,
        vaultwarden_server_url=vw_url,
        vaultwarden_email=vw_email,
        vaultwarden_skip_admin_seed=vw_skip_admin_seed,
        vaultwarden_skip_runner_seed=vw_skip_runner_seed,
        source_path=path,
    )


def validate_enabled_apps_exist(
    catalog: Catalog,
    registry_app_names: list[str],
) -> None:
    """Raise CatalogError if any enabled app is not in the
    registry. Called by the orchestrator after the registry
    has been populated.
    """
    registry_set = set(registry_app_names)
    unknown = [
        n
        for n in catalog.enabled_app_names()
        if n not in registry_set
    ]
    if unknown:
        raise CatalogError(
            f"catalog at {catalog.source_path} enables unknown apps: "
            f"{unknown}. Available: {sorted(registry_set)}"
        )


__all__ = [
    "AppConfig",
    "Catalog",
    "CatalogError",
    "ShippedApp",
    "ShippedCatalog",
    "load_catalog",
    "load_shipped_catalog",
    "validate_enabled_apps_exist",
]
