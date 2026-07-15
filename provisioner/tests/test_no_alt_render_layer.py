"""test_no_alt_render_layer — WP10 static guard.

WP10 ships a single canonical render layer:

  * `provisioner/lib/render_values.py` exposes
    `render_for_app(...)` (deep-merge + write) and
    `render_path(...)` (path-only).
  * `BaseApp._render_for_apply(...)` is the only
    in-class entry point apps reach for.
  * `BaseApp._values_file` / `_rendered_values_file`
    (the WP9 siblings) are path-only — they MUST NOT
    write or merge.

A future contributor who hand-rolls a YAML write
(`yaml.safe_dump(...)`, `json.dumps(..., indent=2)`
fed to `Path.write_text`, f-string YAML, etc.) inside
an app module drifts the single source of truth and
trips the operator's diff. The lint-style test below
fails the build on any such reappearance.

Allowed sites:

  * `render_values.py` itself
  * `BaseApp._render_for_apply(...)` body (delegates
    to `render_for_app`; that's the canonical entry
    point)
  * comment lines + docstrings
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"
RENDER_VALUES = PROVISIONER / "provisioner" / "lib" / "render_values.py"

# Patterns that would indicate "an app is rendering
# values itself." Forbid them. They're narrow enough
# to avoid false positives on legitimate yaml/dump
# usages elsewhere.
WP10_FORBIDDEN_PATTERNS = [
    (
        "yaml.safe_dump(yaml.load(...)) or yaml.dump(",
        re.compile(r"\byaml\.(safe_dump|dump)\b"),
    ),
    (
        "yaml.safe_load(...) to re-emit (post-merge write loop)",
        re.compile(r"\byaml\.safe_load\b"),
    ),
]


def _app_py_files() -> list[Path]:
    """Every `apps/*.py` except `__init__.py` and
    `base.py` (canonical helpers live on `BaseApp`).
    """
    return sorted(
        path
        for path in (APPS_DIR).glob("*.py")
        if path.name not in ("__init__.py", "base.py")
    )


def _permitted_lines_for(app_py: Path) -> set[tuple[str, int]]:
    """Lines in `base.py` that are allowed by the
    WP10 helper. Currently only the body of
    `_render_for_apply` is permitted — that's the
    canonical entry point. The single static-method
    body `VaultwardenK8sSyncApp._render_values` is
    *not* a YAML writer (it reads a committed file,
    mutates a deep-copied dict, and writes to a
    sibling). WP10 permits it because the WP9 helper
    leaves that site as the only `ctx`-less
    alternative.

    Returns: set of (`file`, `line`) tuples that are
    allowed to contain a forbidden pattern.
    """
    return set()


@pytest.mark.parametrize("label,pattern", WP10_FORBIDDEN_PATTERNS)
def test_apps_do_not_invent_alt_render_layer(
    label: str,
    pattern: re.Pattern[str],
) -> None:
    """Every `apps/*.py` is scanned for forbidden
    patterns. Any hit fails with the offending file,
    line, and code excerpt.
    """
    offenders: list[tuple[str, int, str]] = []
    for app_py in _app_py_files():
        for line_no, line in enumerate(
            app_py.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = line.lstrip()
            # Comment + docstring skip (these
            # legitimately reference yaml/yaml.dump).
            if stripped.startswith("#"):
                continue
            if (
                line.lstrip().startswith(('"""', "'''"))
                or line.strip().endswith(('"""', "'''"))
            ):
                continue
            if pattern.search(line):
                offenders.append(
                    (app_py.name, line_no, line.strip())
                )
    offenders = [
        o for o in offenders
        if (o[0], o[1]) not in _permitted_lines_for(
            APPS_DIR / o[0]
        )
    ]
    assert not offenders, (
        f"Forbidden post-WP10 alt-render pattern: "
        f"{label!r}. All YAML writes for the catalog "
        f"must route through "
        f"`provisioner.lib.render_values.render_for_app(...)` "
        f"(reached for via `BaseApp._render_for_apply(...)`). "
        f"Offenders: {offenders}"
    )
