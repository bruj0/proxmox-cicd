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
        extra: dict[str, str] = {
            str(k): str(v) for k, v in cfg.items() if k != "enabled"
        }
        apps[str(name)] = AppConfig(
            enabled=enabled_raw, extra=extra
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
    "load_catalog",
    "validate_enabled_apps_exist",
]
