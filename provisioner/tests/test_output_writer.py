"""WP7 tests — output_writer (apps.json contract)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from provisioner.lib.apps import AppApplyResult
from provisioner.lib.output_writer import (
    APPS_JSON_VERSION,
    build_apps_json,
    load_apps_json,
    write_apps_json,
)


def test_build_apps_json_shape() -> None:
    results = [
        AppApplyResult(
            app_name="gitea",
            namespace="gitea",
            release="gitea",
            chart_version="12.0.0",
            image_version="1.26.x",
            ingress_host="gitea.bruj0.net",
        ),
        AppApplyResult(
            app_name="vaultwarden-k8s-sync",
            namespace="vaultwarden-kubernetes-secrets",
            release="vaultwarden-kubernetes-secrets",
            chart_version="2.0.0",
            image_version="2.0.0",
            ingress_host=None,
        ),
    ]
    out = build_apps_json("cicd", results)
    assert out.cluster_name == "cicd"
    assert out.version == APPS_JSON_VERSION
    assert out.applied_at.endswith(("Z", "+00:00"))  # ISO UTC
    assert len(out.apps) == 2
    assert out.apps[0]["name"] == "gitea"
    assert out.apps[0]["ingress_host"] == "gitea.bruj0.net"
    assert out.apps[1]["ingress_host"] is None


def test_write_apps_json_creates_file_and_chmods(tmp_path: Path) -> None:
    results = [
        AppApplyResult(
            app_name="gitea",
            namespace="gitea",
            release="gitea",
            chart_version="12.0.0",
            image_version="1.26.x",
            ingress_host="gitea.bruj0.net",
        ),
    ]
    path = write_apps_json(tmp_path, "cicd", results)
    assert path.exists()
    # Mode is 0600 (per the writer).
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    payload = json.loads(path.read_text())
    assert payload["cluster_name"] == "cicd"
    assert payload["version"] == APPS_JSON_VERSION
    assert len(payload["apps"]) == 1


def test_write_apps_json_creates_parent_dirs(tmp_path: Path) -> None:
    """No infra/clusters/<n>/ yet — writer must mkdir."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    results = [
        AppApplyResult(
            app_name="gitea",
            namespace="gitea",
            release="gitea",
            chart_version="12.0.0",
            image_version="1.26.x",
            ingress_host="gitea.bruj0.net",
        ),
    ]
    path = write_apps_json(repo, "cicd", results)
    assert path.exists()
    assert path == repo / "infra" / "clusters" / "cicd" / "apps.json"


def test_load_apps_json_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_apps_json(tmp_path, "cicd") is None


def test_load_apps_json_roundtrips(tmp_path: Path) -> None:
    results = [
        AppApplyResult(
            app_name="gitea",
            namespace="gitea",
            release="gitea",
            chart_version="12.0.0",
            image_version="1.26.x",
            ingress_host="gitea.bruj0.net",
        ),
    ]
    write_apps_json(tmp_path, "cicd", results)
    loaded = load_apps_json(tmp_path, "cicd")
    assert loaded is not None
    assert loaded.cluster_name == "cicd"
    assert loaded.version == APPS_JSON_VERSION
    assert len(loaded.apps) == 1
    assert loaded.apps[0]["name"] == "gitea"
    assert loaded.apps[0]["chart_version"] == "12.0.0"


def test_apps_json_schema_matches_plan() -> None:
    """Pins the schema in code so a reader can rely on it."""
    results: list[AppApplyResult] = []
    out = build_apps_json("cicd", results)
    schema_keys = sorted(out.to_dict().keys())
    assert schema_keys == sorted(
        ["version", "cluster_name", "applied_at", "apps"]
    )


def test_apps_json_is_gitignored() -> None:
    """The file we write must not be checked into git."""
    gitignore = Path(".gitignore")
    if not gitignore.exists():
        pytest.skip("no .gitignore in this tree")
    text = gitignore.read_text()
    assert "apps.json" in text


@pytest.fixture(autouse=True)
def _chdir_to_repo() -> None:
    """Some tests read .gitignore from CWD. Make sure we're at the repo root."""
    os.chdir(
        "/home/bruj0/projects/proxmox/proxmox-cicd"
    )
