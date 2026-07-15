"""test_render_values — WP10 helper contract.

WP10 ships a `provisioner/lib/render_values.py` helper
that deep-merges the shipped defaults with the
per-cluster overlay for one app and writes the result
to a render cache:

    <ctx.repo_root>/.proxmox-cicd/rendered/<cluster>/<app>.yaml

The orchestrator catches the helper before any
apply/destroy logic runs — see the
`cicdctl render` CLI subcommand wired in WP10.

Five cases:

  * shipped defaults only — no cluster overlay. File
    gets the shipped dict verbatim (or empty if no
    defaults are declared).
  * cluster overlay replaces top-level keys.
  * nested dict deep-merge (a partial override at
    `path.foo` doesn't clobber `path.bar`).
  * the rendered output goes to
    `.proxmox-cicd/rendered/<cluster>/<app>.yaml`
    and the directory is created on demand.
  * apps with no defaults and no overlay raise a
    clear error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from provisioner.lib.render_values import (
    NoShippedDefaultsError,
    render_for_app,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A clean repo root. The helper writes the
    rendered file under `.proxmox-cicd/rendered/`
    relative to `ctx.repo_root`."""
    return tmp_path


def test_render_uses_shipped_defaults_when_no_cluster_file(
    repo_root: Path,
) -> None:
    """No per-cluster overlay file present; the
    rendered output equals the shipped defaults
    verbatim (a deep-copy, not a reference — the
    helper must not mutate the shipped dict).
    """
    shipped_defaults = {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "ingress": {"enabled": True, "host": "gitea.bruj0.net"},
    }
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="cicd",
        repo_root=repo_root,
        shipped_defaults=shipped_defaults,
        cluster_overlay=None,
    )
    assert out_path == repo_root / ".proxmox-cicd" / "rendered" / "cicd" / "gitea.yaml"
    assert out_path.exists()
    loaded = yaml.safe_load(out_path.read_text())
    assert loaded == shipped_defaults
    # And the shipped dict hasn't been mutated.
    assert shipped_defaults == {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "ingress": {"enabled": True, "host": "gitea.bruj0.net"},
    }


def test_render_overlays_cluster_values_on_shipped_defaults(
    repo_root: Path,
) -> None:
    """A cluster overlay that lists a key present in
    the shipped dict replaces it. The result is the
    post-merge shape (shipped + cluster_wins)."""
    shipped_defaults = {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "ingress": {"enabled": True, "host": "gitea.bruj0.net"},
    }
    cluster_overlay = {
        "ingress": {"host": "gitea.staging.bruj0.net"},
    }
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="staging",
        repo_root=repo_root,
        shipped_defaults=shipped_defaults,
        cluster_overlay=cluster_overlay,
    )
    loaded = yaml.safe_load(out_path.read_text())
    # `ingress.host` overridden; `ingress.enabled` preserved.
    assert loaded == {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "ingress": {"enabled": True, "host": "gitea.staging.bruj0.net"},
    }


def test_render_deep_merges_nested_keys(repo_root: Path) -> None:
    """Nested dicts merge recursively. A partial
    override at `path.foo` leaves `path.bar` at the
    shipped value."""
    shipped_defaults = {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "tls": {"enabled": True, "issuer": "letsencrypt-prod"},
    }
    cluster_overlay = {
        "tls": {"issuer": "letsencrypt-staging"},
    }
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="staging",
        repo_root=repo_root,
        shipped_defaults=shipped_defaults,
        cluster_overlay=cluster_overlay,
    )
    loaded = yaml.safe_load(out_path.read_text())
    # `tls.issuer` overridden; `tls.enabled` preserved
    # from shipped.
    assert loaded == {
        "image": {"tag": "1.26.x", "pullPolicy": "IfNotPresent"},
        "tls": {"enabled": True, "issuer": "letsencrypt-staging"},
    }


def test_render_writes_to_gitignored_temp_dir(repo_root: Path) -> None:
    """The output directory is created on demand
    under `.proxmox-cicd/rendered/<cluster>/`. The
    `.proxmox-cicd/` prefix is the operator's
    getignored scratch area (already in `.gitignore`
    for the staging files).
    """
    # Sanity: nothing exists before.
    render_dir = repo_root / ".proxmox-cicd" / "rendered"
    assert not render_dir.exists()

    out_path = render_for_app(
        app_name="gitea",
        cluster_name="cicd",
        repo_root=repo_root,
        shipped_defaults={"key": "value"},
        cluster_overlay=None,
    )
    assert out_path.exists()
    # Parent dirs created.
    assert render_dir.is_dir()
    assert (render_dir / "cicd").is_dir()


def test_render_raises_when_shipped_app_has_no_default_values_file(
    repo_root: Path,
) -> None:
    """Apps that ship no defaults (and have no
    cluster overlay) raise `NoShippedDefaultsError`.
    The error message names the app so the operator
    can grep the audit log."""
    with pytest.raises(NoShippedDefaultsError) as exc_info:
        render_for_app(
            app_name="gitea",
            cluster_name="cicd",
            repo_root=repo_root,
            shipped_defaults={},  # empty shipped defaults
            cluster_overlay=None,
        )
    assert "gitea" in str(exc_info.value)


# ----- additional safety cases -----------------------------------------


def test_render_cluster_overlay_only_is_enough(repo_root: Path) -> None:
    """If shipped defaults are empty but the cluster
    supplies an overlay, the rendered output is the
    overlay (no error). The `NoShippedDefaultsError`
    only fires when *both* sides are empty.
    """
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="cicd",
        repo_root=repo_root,
        shipped_defaults={},
        cluster_overlay={"ingress": {"host": "gitea.bruj0.net"}},
    )
    loaded = yaml.safe_load(out_path.read_text())
    assert loaded == {"ingress": {"host": "gitea.bruj0.net"}}


def test_render_overlay_replaces_scalar_with_dict(repo_root: Path) -> None:
    """If the cluster overlay at a key is a dict and
    the shipped value is a scalar, the dict wins
    entirely (no recursion through scalars). Same
    rule as `BaseApp._deep_merge`."""
    shipped_defaults = {"key": "scalar"}
    cluster_overlay = {"key": {"nested": True}}
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="cicd",
        repo_root=repo_root,
        shipped_defaults=shipped_defaults,
        cluster_overlay=cluster_overlay,
    )
    loaded = yaml.safe_load(out_path.read_text())
    assert loaded == {"key": {"nested": True}}


def test_render_yaml_round_trip_is_stable(repo_root: Path) -> None:
    """The rendered file is valid YAML and round-trips
    through `yaml.safe_load` without information
    loss (the dict the operator will see when they
    inspect the file equals the dict the orchestrator
    passes to helm)."""
    shipped_defaults = {
        "replicaCount": 1,
        "image": {"tag": "1.26.x"},
        "lists": ["a", "b", "c"],
    }
    out_path = render_for_app(
        app_name="gitea",
        cluster_name="cicd",
        repo_root=repo_root,
        shipped_defaults=shipped_defaults,
        cluster_overlay=None,
    )
    text = out_path.read_text()
    # Round-trip
    parsed = yaml.safe_load(text)
    re_dumped = yaml.safe_dump(parsed)
    re_parsed = yaml.safe_load(re_dumped)
    assert parsed == re_parsed
