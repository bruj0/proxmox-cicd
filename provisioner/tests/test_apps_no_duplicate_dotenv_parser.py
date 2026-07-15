"""test_apps_no_duplicate_dotenv_parser — WP11 static guard.

After WP11 the canonical `.env` parser lives on
`BaseApp` (see `apps/base.py`). A future contributor
adding a new app should reach for
`self._load_dotenv(repo_root)` (or
`BaseApp._parse_dotenv(text)`) — not roll a private
`_parse_dotenv` that drifts from the canonical one.

This test fails the build if any `apps/*.py` file
defines its own `_parse_dotenv` (the only allowed
definition is the one on `BaseApp`, which lives in
`apps/base.py`, not `apps/*.py` proper).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"


def _app_py_files() -> list[Path]:
    # The only legal home for `_parse_dotenv` is
    # `apps/base.py` (the canonical parser). Filter it
    # out so the guard is "no app *other than* BaseApp
    # defines a private parser".
    return sorted(
        p
        for p in APPS_DIR.glob("*.py")
        if p.name not in ("__init__.py", "base.py")
    )


@pytest.mark.parametrize(
    "app_py", _app_py_files(), ids=lambda p: p.name
)
def test_app_does_not_define_parse_dotenv(app_py: Path) -> None:
    """No `apps/*.py` may define `_parse_dotenv`.

    Apps reach for `self._load_dotenv(repo_root)` (or
    `BaseApp._parse_dotenv(text)` if they already have
    the file contents in hand). A private parser that
    drifts from the canonical one is a WP11 violation.
    """
    src = app_py.read_text(encoding="utf-8")
    # Match `def _parse_dotenv` at module level (allow
    # leading whitespace; reject inside string literals
    # only via a simple line-level heuristic — good
    # enough for this static guard, the runtime guard
    # is the BaseApp-level one).
    offenders = [
        line.strip()
        for line in src.splitlines()
        if line.lstrip().startswith("def _parse_dotenv")
        or line.lstrip().startswith("async def _parse_dotenv")
    ]
    assert not offenders, (
        f"{app_py.name} defines a private `_parse_dotenv`; "
        f"use `self._load_dotenv(repo_root)` or "
        f"`BaseApp._parse_dotenv(text)` instead (WP11)."
    )
