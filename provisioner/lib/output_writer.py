"""output_writer — write infra/clusters/<name>/apps.json.

The apps.json file is the canonical handoff from proxmox-cicd
to any downstream consumer (other apps in the catalog, or
external automation that wants to know "what's installed
where"). Mirrors proxmox-vms/infra/clusters/<name>/output.json
in shape and intent.

The single source of truth for what's "applied" is the
orchestrator's list of `AppApplyResult` from each enabled
app's `.apply()`. The orchestrator calls `write_apps_json`
exactly once at the end of a successful run.

Schema (v1, 2026-07-10):

  {
    "cluster_name": "cicd",
    "applied_at": "2026-07-10T13:42:01.234Z",
    "apps": [
      {
        "name": "gitea",
        "namespace": "gitea",
        "release": "gitea",
        "chart_version": "12.0.0",
        "image_version": "1.26.x",
        "ingress_host": "gitea.bruj0.net"
      },
      ...
    ]
  }

The file is gitignored (see .gitignore) so a re-run can
overwrite it freely.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .apps import AppApplyResult

APPS_JSON_VERSION = "1"


@dataclass
class AppsJson:
    """Typed wrapper around the apps.json payload."""

    cluster_name: str
    applied_at: str
    apps: list[dict[str, Any]]
    version: str = APPS_JSON_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cluster_name": self.cluster_name,
            "applied_at": self.applied_at,
            "apps": self.apps,
        }


def build_apps_json(
    cluster_name: str,
    applied: list[AppApplyResult],
) -> AppsJson:
    """Build an AppsJson from the orchestrator's results."""
    return AppsJson(
        cluster_name=cluster_name,
        applied_at=datetime.now(UTC).isoformat(),
        apps=[
            {
                "name": r.app_name,
                "namespace": r.namespace,
                "release": r.release,
                "chart_version": r.chart_version,
                "image_version": r.image_version,
                "ingress_host": r.ingress_host,
            }
            for r in applied
        ],
    )


def write_apps_json(
    repo_root: Path,
    cluster_name: str,
    applied: list[AppApplyResult],
) -> Path:
    """Build + write apps.json. Returns the path it wrote to."""
    apps_json = build_apps_json(cluster_name, applied)
    path = repo_root / "infra" / "clusters" / cluster_name / "apps.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(apps_json.to_dict(), indent=2) + "\n")
    # Also chmod 0600 — this file may include hostname
    # metadata that we don't want world-readable.
    path.chmod(0o600)
    return path


def load_apps_json(repo_root: Path, cluster_name: str) -> AppsJson | None:
    """Read apps.json if it exists; else return None.

    Used by `cicdctl status` to seed the table with
    previously-installed app metadata (so the table header
    can include the cluster's apps even when the helm list
    call hasn't been made yet).
    """
    path = repo_root / "infra" / "clusters" / cluster_name / "apps.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return AppsJson(
        cluster_name=payload["cluster_name"],
        applied_at=payload["applied_at"],
        apps=payload["apps"],
        version=payload.get("version", APPS_JSON_VERSION),
    )


__all__ = [
    "APPS_JSON_VERSION",
    "AppsJson",
    "asdict",
    "build_apps_json",
    "load_apps_json",
    "write_apps_json",
]
