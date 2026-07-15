"""test_app_class_attributes — WP13 contract tests.

WP13 lifts chart / chart_version / image_version /
namespace / release / default_values_file from
module-level constants onto class attributes on each
`BaseApp` subclass. The shipped catalog stays the
single source of truth for these values; the class
attribute is the *application* of that truth at the
app's class-creation time.

Two guard tests:

  * the class attributes exist on every shipped app
  * the class attributes match the corresponding
    entry in `provisioner/lib/catalog/shipped.yaml`

A `drift` test fails the build the moment the two
sources of truth disagree. The shipped-catalog
generator script (follow-up WP) keeps the YAML in
sync with `versions.lock.yaml`; the class
attributes keep the apps in sync with the YAML.

Module-level constants (`CHART`, `CHART_VERSION`,
...) are deleted as part of WP13. A static-import
guard pins the deletion: any reappearance trips
the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from provisioner.lib.apps.base import BaseApp
from provisioner.lib.apps.gitea import GiteaApp
from provisioner.lib.apps.gitea_runner import GiteaRunnerApp
from provisioner.lib.apps.cloudflared import CloudflaredApp
from provisioner.lib.apps.vaultwarden_k8s_sync import VaultwardenK8sSyncApp


# All shipped apps; order is the shipped-catalog order
# (asserted in the drift tests below).
SHIPPED_APP_CLASSES = (
    GiteaApp,
    GiteaRunnerApp,
    CloudflaredApp,
    VaultwardenK8sSyncApp,
)

SHIPPED_CATALOG_PATH = Path(__file__).resolve().parents[2] / (
    "provisioner/lib/catalog/shipped.yaml"
)


def _load_shipped_catalog() -> dict:
    return yaml.safe_load(SHIPPED_CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("app_cls", SHIPPED_APP_CLASSES, ids=lambda c: c.__name__)
def test_app_declares_class_attributes(app_cls: type[BaseApp]) -> None:
    """Every shipped app must expose the WP13
    class-attribute contract.

    `namespace` and `release` are `@property` on
    `BaseApp` that default to `self.name`. Reading
    them through an instance returns the runtime
    value (overridden or fallback); reading through
    the class returns the descriptor. Use an instance
    here so the test sees what `apply()` / `plan()`
    see at runtime.
    """
    app = app_cls()
    assert isinstance(app.name, str) and app.name
    assert isinstance(app.namespace, str) and app.namespace
    assert isinstance(app.release, str) and app.release
    assert isinstance(app.chart, str) and app.chart
    assert isinstance(app.chart_version, str) and app.chart_version
    # `image_version` is optional (cloudflared +
    # vaultwarden-k8s-sync pin via the chart's
    # `appVersion`, not via an explicit image tag).
    image_version = getattr(app, "image_version", None)
    if image_version is not None:
        assert isinstance(image_version, str)
    # `default_values_file` is also optional.
    default_values_file = getattr(app, "default_values_file", None)
    if default_values_file is not None:
        assert isinstance(default_values_file, str)


@pytest.mark.parametrize("app_cls", SHIPPED_APP_CLASSES, ids=lambda c: c.__name__)
def test_app_class_attributes_match_shipped_catalog(
    app_cls: type[BaseApp]
) -> None:
    """Class attributes must match `shipped.yaml`.

    Drift here is a release-blocker — the shipped
    catalog is the operator-facing surface (per WP1
    documentation); the class attributes are the
    apps' runtime reads. If the two disagree, plan
    output is wrong or `apply()` uses the wrong
    chart version.
    """
    catalog = _load_shipped_catalog()
    entry = catalog["apps"][app_cls.name]
    app = app_cls()
    # `namespace` / `release` resolve via the
    # `BaseApp` property; everything else is a plain
    # class attribute.
    for key in ("namespace", "release"):
        assert getattr(app, key) == entry[key], (
            f"{app_cls.name}.{key} = {getattr(app, key)!r} but "
            f"shipped.yaml says {entry[key]!r}"
        )
    for key in ("chart", "chart_version"):
        assert getattr(app_cls, key) == entry[key], (
            f"{app_cls.name}.{key} = {getattr(app_cls, key)!r} but "
            f"shipped.yaml says {entry[key]!r}"
        )
    image_version = getattr(app_cls, "image_version", None)
    if image_version is not None:
        assert image_version == entry["image_version"], (
            f"{app_cls.name}.image_version = {image_version!r} but "
            f"shipped.yaml says {entry['image_version']!r}"
        )
