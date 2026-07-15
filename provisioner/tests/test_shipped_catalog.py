"""test_shipped_catalog — WP1 regression tests for the shipped catalog.

WP1 of the GroupSpec plan introduces a two-layer catalog:
the codebase ships `provisioner/lib/catalog/shipped.yaml`
listing every app this version knows about, and the
per-cluster `infra/clusters/<name>/catalog.yaml` becomes
a thin value-override layer.

These tests pin three invariants:

  1. `load_shipped_catalog(path)` parses the bundled YAML
     and returns a `ShippedCatalog` with the documented
     apps.
  2. `Catalog.from_shipped_and_cluster(shipped, cluster)`
     deep-merges per the rule in §5.2 and applies
     per-cluster `enabled:` flags.
  3. A cluster catalog referencing an app not in the
     shipped catalog raises `CatalogError` listing the
     unknown name(s).

The shipped YAML is the **single source of truth** for
"what apps this version ships"; a future contributor
can't silently add an app by editing the per-cluster
catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from provisioner.lib.catalog import (
    Catalog,
    CatalogError,
)


SHIPPED_YAML = Path(
    "provisioner/lib/catalog/shipped.yaml"
)


def _write_minimal_shipped(
    path: Path,
    apps: dict[str, dict[str, object]] | None = None,
    version: str = "0.3.0",
) -> None:
    """Write a tiny shipped.yaml with the given apps.

    Used by tests that don't need the full shipped catalog;
    keeps the test surface narrow. Nested mappings are
    rendered via `yaml.safe_dump` so dicts/lists/bools
    land as proper YAML, not Python repr.
    """
    if apps is None:
        apps = {
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
            },
        }
    body: dict[str, object] = {"version": version, "apps": apps}
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def _write_minimal_cluster(
    path: Path,
    cluster_name: str = "test-cluster",
    apps: dict[str, dict[str, object]] | None = None,
    ingress_base_domain: str = "example.net",
) -> None:
    """Write a tiny per-cluster catalog.yaml.

    Defaults to enabling whatever's in `apps` (so tests can
    exercise the merge rule without spelling out `enabled: true`
    for every app). Nested mappings are rendered via
    `yaml.safe_dump` so dicts/lists/bools land as proper
    YAML, not Python repr.
    """
    if apps is None:
        apps = {"gitea": {"enabled": True}}
    body: dict[str, object] = {
        "cluster_name": cluster_name,
        "ingress": {"base_domain": ingress_base_domain},
        "apps": apps,
    }
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def test_shipped_yaml_exists() -> None:
    """The shipped catalog YAML is committed to the repo.

    Editing the shipped catalog is a code change; the file
    must exist before the orchestrator can load it.
    """
    assert SHIPPED_YAML.exists(), (
        f"shipped catalog not found at {SHIPPED_YAML}. "
        f"WP1 requires provisioner/lib/catalog/shipped.yaml "
        f"to be committed to the codebase."
    )


def test_shipped_yaml_parses() -> None:
    """The shipped YAML is well-formed (no syntax errors,
    parses to a non-empty `apps` mapping).

    A typo in the shipped YAML would break every
    `cicdctl` invocation, so this test runs first as a
    smoke check.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped = load_shipped_catalog(SHIPPED_YAML)
    assert shipped.version != ""
    assert len(shipped.apps) >= 1


def test_shipped_catalog_lists_four_shipped_apps() -> None:
    """The shipped catalog lists the four apps this
    version of `proxmox-cicd` ships: `gitea`,
    `gitea-runner`, `vaultwarden-k8s-sync`, and
    `cloudflared`.

    A future contributor who adds a 5th app must:
      1. add the app file under `provisioner/lib/apps/`
      2. add the app to the shipped catalog (this test)
      3. optionally enable it in `infra/clusters/<n>/catalog.yaml`

    Steps 1 and 2 are atomic in a code review; step 3
    is per-cluster.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped = load_shipped_catalog(SHIPPED_YAML)
    assert "gitea" in shipped.apps
    assert "gitea-runner" in shipped.apps
    assert "vaultwarden-k8s-sync" in shipped.apps
    assert "cloudflared" in shipped.apps


def test_shipped_app_required_keys() -> None:
    """Each shipped app entry has the four keys the
    orchestrator relies on: `namespace`, `release`,
    `chart`, `chart_version`.

    (WP13 reads `image_version` too, but WP1 lands
    before WP13, so the WP1 acceptance doesn't require
    `image_version`.)
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped = load_shipped_catalog(SHIPPED_YAML)
    for app_name, app in shipped.apps.items():
        for key in ("namespace", "release", "chart", "chart_version"):
            assert getattr(app, key), (
                f"shipped app {app_name!r} missing required "
                f"key {key!r}"
            )


def test_load_shipped_catalog_matches_yaml_on_disk() -> None:
    """`load_shipped_catalog` returns exactly what the
    YAML contains — no defaults, no derived fields.

    The merge rule (§5.2) does the work; the loader is
    a thin pass-through.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    _write_minimal_shipped(
        SHIPPED_YAML.parent / "shipped.test.yaml",
        apps={
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
            },
            "gitea-runner": {
                "description": "Runner.",
                "namespace": "gitea-runner",
                "release": "gitea-runner",
                "chart": "./infra/charts/gitea-runner",
                "chart_version": "0.2.0",
                "image_version": "1.0.8-dind",
            },
        },
    )
    shipped = load_shipped_catalog(
        SHIPPED_YAML.parent / "shipped.test.yaml"
    )
    assert set(shipped.apps.keys()) == {"gitea", "gitea-runner"}
    assert shipped.apps["gitea"].namespace == "gitea"
    assert shipped.apps["gitea-runner"].chart == (
        "./infra/charts/gitea-runner"
    )


def test_merge_shipped_with_cluster_overrides_values() -> None:
    """`Catalog.from_shipped_and_cluster` deep-merges
    per the rule in §5.2: per-cluster `values:` overlay
    on top of shipped `default_values:`.

    A per-cluster value wins on a key-by-key basis; the
    shipped defaults are the fall-through.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped_path = SHIPPED_YAML.parent / "shipped.merge.yaml"
    cluster_path = SHIPPED_YAML.parent / "cluster.merge.yaml"

    _write_minimal_shipped(
        shipped_path,
        apps={
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
                "default_values": {
                    "replicaCount": 1,
                    "persistence": {"size": "5Gi"},
                    "ingress": {"hostname": "gitea.example.net"},
                },
            },
        },
    )
    _write_minimal_cluster(
        cluster_path,
        apps={
            "gitea": {
                "enabled": True,
                "values": {"replicaCount": 3},
            },
        },
    )

    from provisioner.lib.catalog import (
        load_catalog,
    )

    shipped = load_shipped_catalog(shipped_path)
    cluster = load_catalog(cluster_path, "test-cluster")
    merged = Catalog.from_shipped_and_cluster(shipped, cluster)

    # replicaCount comes from per-cluster (overrides shipped default).
    assert merged.apps["gitea"].values["replicaCount"] == 3
    # persistence.size comes from shipped default (cluster didn't override).
    assert merged.apps["gitea"].values["persistence"]["size"] == "5Gi"
    # ingress.hostname comes from shipped default.
    assert merged.apps["gitea"].values["ingress"]["hostname"] == (
        "gitea.example.net"
    )


def test_merge_raises_when_cluster_references_unknown_app() -> None:
    """A cluster catalog referencing an app not in the
    shipped catalog raises `CatalogError` with the
    unknown name(s).

    This is the rule from §5.2: the codebase-shipped
    catalog is the single source of truth for known
    apps. The cluster catalog can only enable/disable
    apps that exist in the shipped catalog. A typo or
    a stale reference fails fast at orchestrator startup.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped_path = SHIPPED_YAML.parent / "shipped.unknown.yaml"
    cluster_path = SHIPPED_YAML.parent / "cluster.unknown.yaml"

    _write_minimal_shipped(
        shipped_path,
        apps={
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
            },
        },
    )
    _write_minimal_cluster(
        cluster_path,
        apps={
            "gitea": {"enabled": True},
            "ghost-app": {"enabled": True},  # not in shipped
        },
    )

    from provisioner.lib.catalog import load_catalog

    shipped = load_shipped_catalog(shipped_path)
    cluster = load_catalog(cluster_path, "test-cluster")

    with pytest.raises(CatalogError, match="ghost-app"):
        Catalog.from_shipped_and_cluster(shipped, cluster)


def test_cluster_app_disable_drops_from_merged_catalog() -> None:
    """A per-cluster `enabled: false` (or absent) drops
    the app from the merged catalog's `enabled_app_names()`.

    The merge preserves the YAML shape but flips
    `enabled` to `False` for apps the cluster
    disables.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped_path = SHIPPED_YAML.parent / "shipped.disable.yaml"
    cluster_path = SHIPPED_YAML.parent / "cluster.disable.yaml"

    _write_minimal_shipped(
        shipped_path,
        apps={
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
            },
            "cloudflared": {
                "description": "Cloudflare Tunnel.",
                "namespace": "cloudflared",
                "release": "cloudflare-tunnel-remote",
                "chart": "oci://ghcr.io/antoniolago/charts/cloudflare-tunnel-remote",
                "chart_version": "0.4.0",
                "image_version": "0.4.0",
            },
        },
    )
    _write_minimal_cluster(
        cluster_path,
        apps={
            "gitea": {"enabled": True},
            "cloudflared": {"enabled": False},
        },
    )

    from provisioner.lib.catalog import load_catalog

    shipped = load_shipped_catalog(shipped_path)
    cluster = load_catalog(cluster_path, "test-cluster")
    merged = Catalog.from_shipped_and_cluster(shipped, cluster)

    assert "gitea" in merged.enabled_app_names()
    assert "cloudflared" not in merged.enabled_app_names()


def test_cluster_app_absent_defaults_to_disabled() -> None:
    """An app not listed in the cluster catalog is treated
    as `enabled: false`. The shipped catalog advertises
    the app; the cluster catalog opts in (or out).

    This mirrors today's behaviour: a per-cluster
    catalog that doesn't list an app leaves it disabled.
    """
    from provisioner.lib.catalog import load_shipped_catalog

    shipped_path = SHIPPED_YAML.parent / "shipped.absent.yaml"
    cluster_path = SHIPPED_YAML.parent / "cluster.absent.yaml"

    _write_minimal_shipped(
        shipped_path,
        apps={
            "gitea": {
                "description": "Gitea.",
                "namespace": "gitea",
                "release": "gitea",
                "chart": "oci://docker.gitea.com/charts/gitea",
                "chart_version": "12.0.0",
                "image_version": "1.26.x",
            },
        },
    )
    _write_minimal_cluster(cluster_path, apps={"gitea": {"enabled": True}})

    from provisioner.lib.catalog import load_catalog

    shipped = load_shipped_catalog(shipped_path)
    cluster = load_catalog(cluster_path, "test-cluster")
    merged = Catalog.from_shipped_and_cluster(shipped, cluster)

    # gitea is enabled (cluster opt-in).
    assert "gitea" in merged.enabled_app_names()
    # Even if shipped catalog has other apps, cluster absence
    # disables them.
    assert len(merged.enabled_app_names()) == 1
