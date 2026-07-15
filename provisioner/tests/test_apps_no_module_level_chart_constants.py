"""test_apps_no_module_level_chart_constants — WP13 static guard.

After WP13 chart constants (`CHART`, `CHART_VERSION`,
`IMAGE_TAG`/`APP_VERSION`, `NAMESPACE`, `RELEASE`,
`DEFAULT_VALUES_FILE`) live as class attributes on
each `BaseApp` subclass. Module-level constants are
gone from the apps (they remain valid in `base.py`
for tests, and on apps whose constants are
per-instance contract — e.g. `RUNNER_CONFIG_SECRET`
in gitea_runner — but those are not in the WP13
set).

The expected pattern for reading a version is:

    GiteaApp.chart_version      # class attr
    app_instance.chart_version  # instance attr
        (= self.chart_version inside methods)

A future contributor who copies the pre-WP13 module-
level constants back into a new app would silently
drift from the shipped catalog. The test below
fails the build on any such reappearance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"

# WP13 — module-level constants that should not exist
# anywhere in `apps/*.py` (they live as class attributes
# now).
WP13_REMOVED_CONSTANTS = (
    "CHART",
    "CHART_VERSION",
    "IMAGE_TAG",
    "APP_VERSION",
    "RELEASE",
    "DEFAULT_VALUES_FILE",
)


def _app_py_files() -> list[Path]:
    return sorted(p for p in APPS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize(
    "constant", WP13_REMOVED_CONSTANTS, ids=lambda c: c
)
def test_no_module_level_constant_in_apps(constant: str) -> None:
    """No `apps/*.py` defines the WP13-removed constants
    at module level. `CloudflaredApp`'s `CHART_TGZ`
    falls under `CHART` (the chart is a vendored
    tarball, the constant held a `Path`) — the
    guard above catches that case via the `CHART`
    entry.
    """
    offenders: list[tuple[str, str]] = []
    for app_py in _app_py_files():
        for line in app_py.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            # Match a top-level `NAME =` form (not
            # `self.NAME =`, not a `class.foo.NAME`).
            if (
                stripped.startswith(f"{constant} =")
                or stripped.startswith(f"{constant}=")
            ):
                offenders.append((app_py.name, line.strip()))
    assert not offenders, (
        f"Module-level `{constant} = ...` is forbidden after WP13. "
        f"Move it to a `BaseApp` subclass as a class attribute "
        f"(`chart`, `chart_version`, `image_version`, `namespace`, "
        f"`release`, `default_values_file`). Offenders: {offenders}"
    )
