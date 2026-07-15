"""test_apps_no_inline_wp9_patterns — WP9 static guard.

After WP9 the canonical helpers on `BaseApp` are the
only allowed way to build these constructs:

  * `SecretKeySelector(...)` blocks → `self._secret_ref(name, key)`
  * `values-rendered.yaml` filename logic → `self._rendered_values_file(ctx)`
    (or the `VaultwardenK8sSyncApp._render_values` static
    helper, which is the one allowed site for the
    inline form because it has no `ctx`)
  * `_values_file(ctx)` from `BaseApp` (replaces per-app
    overrides of the same name)

A future contributor who copies one of these patterns
back into a new app would silently drift from the
canonical form. The lint-style test below fails the
build on any such reappearance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"

# Inline patterns that are forbidden after WP9. The
# test scans `apps/*.py` for each pattern and reports
# any hit. The patterns are intentionally narrow —
# they only match the categories the WP9 refactor
# aimed to centralize.
WP9_FORBIDDEN_PATTERNS = [
    # `SecretKeySelector` literal — apps must reach
    # for `self._secret_ref(...)` instead.
    (
        "SecretKeySelector literal",
        re.compile(r"\bSecretKeySelector\b"),
    ),
]


def _app_py_files() -> list[Path]:
    """Every `apps/*.py` except `__init__.py` and
    `base.py` (the canonical helpers live on BaseApp).

    Filtered files are scanned; `base.py` and
    `__init__.py` are skipped.
    """
    return sorted(
        p
        for p in APPS_DIR.glob("*.py")
        if p.name not in ("__init__.py", "base.py")
    )


@pytest.mark.parametrize(
    "label,pattern",
    WP9_FORBIDDEN_PATTERNS,
    ids=lambda p: p if isinstance(p, str) else p.pattern,
)
def test_app_has_no_inline_pattern(label: str, pattern: re.Pattern[str]) -> None:
    """No `apps/*.py` other than `base.py` may contain
    the inline form. Forward-compat guard so a
    duplicated helper trips the build."""
    offenders: list[tuple[str, str, str]] = []
    for app_py in _app_py_files():
        for line_no, line in enumerate(
            app_py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append((app_py.name, str(line_no), line.strip()))
    assert not offenders, (
        f"Forbidden post-WP9 pattern: {label!r}. "
        f"Use the corresponding `BaseApp` helper instead. "
        f"Offenders: {offenders}"
    )


# ----- values-rendered path construction ------------------------------
#
# WP9 — apps must NOT construct the rendered values
# file path inline. They reach for
# `self._rendered_values_file(ctx)`.
#
# One exception: `VaultwardenK8sSyncApp._render_values`
# is a static helper that takes a `committed_values`
# path directly (no `ctx`); it's the canonical
# renderer and is the one allowed site for the
# inline form. The guard below filters out the body
# of that helper so the canonical path lives in
# exactly one place (inside `_render_values`).
#
# Doc comments referencing the file by name are also
# permitted — the guard targets `Path()`-or-f-string
# path construction specifically.


_VALUES_RENDERED_PATH_PATTERNS = [
    # Path(...) / "...values-rendered.yaml"
    # (any `/` operator on the right-hand side, with
    # the right operand being a `values-rendered.yaml`
    # literal).
    (
        "Path() / '*.values-rendered.yaml'",
        re.compile(
            r"""(?:^|\W)              # word boundary
                / \s*                 # `/` operator (any whitespace)
                ['\"]                 # opening quote
                [^'\"]*?              # path prefix
                values-rendered\.yaml
                ['\"]                 # closing quote
            """,
            re.VERBOSE,
        ),
    ),
    # f-string concatenation: f"{stem}.values-rendered.yaml"
    (
        'f"{stem}.values-rendered.yaml"',
        re.compile(
            r"f['\"][^{}]*\{[^}]+\}\.values-rendered\.yaml['\"]"
        ),
    ),
    # Plain concatenation: 'stem' + ".values-rendered.yaml"
    (
        "'stem' + '.values-rendered.yaml'",
        re.compile(
            r"[a-zA-Z_]+\s*\+\s*['\"]\.values-rendered\.yaml['\"]"
        ),
    ),
]


def _collect_permitted_lines() -> set[tuple[str, int]]:
    """Return the (file, line_no) pairs that are the
    body of `VaultwardenK8sSyncApp._render_values`,
    the one allowed inline-construction site."""
    permitted: set[tuple[str, int]] = set()
    for app_py in _app_py_files():
        if app_py.name != "vaultwarden_k8s_sync.py":
            continue
        in_render_values = False
        for line_no, line in enumerate(
            app_py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.startswith("    def _render_values"):
                in_render_values = True
                continue
            if in_render_values and (
                line.startswith("    def ") or line.startswith("class ")
            ):
                in_render_values = False
            if in_render_values:
                permitted.add((app_py.name, line_no))
    return permitted


@pytest.mark.parametrize(
    "label,pattern",
    _VALUES_RENDERED_PATH_PATTERNS,
    ids=lambda p: p if isinstance(p, str) else p.pattern,
)
def test_app_does_not_construct_rendered_values_path_inline(
    label: str, pattern: re.Pattern[str]
) -> None:
    """No `apps/*.py` may construct the rendered values
    file path inline. Apps reach for
    `self._rendered_values_file(ctx)` from `BaseApp`
    instead.

    Doc comments referencing the file by name are not
    flagged — this guard targets `Path()`-or-f-string
    path construction specifically.

    One exception: `VaultwardenK8sSyncApp._render_values`
    is a static helper that takes a `committed_values`
    path directly (no `ctx`); it's the canonical
    renderer and is the one allowed site for the
    inline form.
    """
    offenders: list[tuple[str, str, str]] = []
    for app_py in _app_py_files():
        for line_no, line in enumerate(
            app_py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            # Skip comment-only lines and docstrings —
            # those reference the filename by name and
            # are legitimate documentation.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if line.lstrip().startswith(('"""', "'''")) or line.strip().endswith(
                ('"""', "'''")
            ):
                continue
            if pattern.search(line):
                offenders.append((app_py.name, str(line_no), line.strip()))
    # Filter out lines inside the canonical
    # `VaultwardenK8sSyncApp._render_values` body —
    # that's the one allowed site for the inline form.
    offenders = [
        o for o in offenders
        if (o[0], int(o[1])) not in _collect_permitted_lines()
    ]
    assert not offenders, (
        f"Forbidden post-WP9 inline values-rendered path: {label!r}. "
        f"Use `self._rendered_values_file(ctx)` instead "
        f"(or move the path-construction into "
        f"`VaultwardenK8sSyncApp._render_values` if no "
        f"`ctx` is in scope). Offenders: {offenders}"
    )
