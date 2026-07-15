"""test_apps_no_appspec_refs — WP15 static guard.

WP15 removes the `AppSpec` Protocol that WP0 left
behind as a runtime-checkable alias. The replacement
is:

    # apps/__init__.py — the one allowed site
    AppSpec = BaseApp

Apps, tests, and (active) docs MUST NOT reference
`AppSpec` directly — they reach for `BaseApp`. The
single allowed reference is the alias line in
`apps/__init__.py`.

The ruff `[tool.ruff.lint]` `forbidden-name`
configuration is the runtime guard for app code. This
test is the *build-time* guard that catches any
reintroduction of `AppSpec` at the module level
(imports + at-runtime references, AST-based).
"""

from __future__ import annotations

import ast
from pathlib import Path

PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"
APPS_INIT = APPS_DIR / "__init__.py"


def _appspec_refs_outside_allowed_site(path: Path) -> list[tuple[int, str]]:
    """AST-based scan: find every line in `path` that
    references the bare name `AppSpec` (Name or
    Attribute) outside any docstring. The line ranges
    of docstrings (module + every class/function) are
    excluded from the search.

    Allowed references: the single `AppSpec = BaseApp`
    alias line in `apps/__init__.py`. That line is
    ast.Assign — the LHS is `AppSpec` (an ast.Name
    in Store context). All other Name/Attribute
    references in code are reported.
    """
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    # Docstring line ranges (module-level + every
    # function/class) excluded from the scan.
    docstring_ranges: list[tuple[int, int]] = []

    if (
        isinstance(tree, ast.Module)
        and tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        docstring_ranges.append(
            (1, tree.body[0].end_lineno or 1)
        )

    def _record_doc(node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if (
            isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_ranges.append(
                (node.lineno, body[0].end_lineno or node.lineno)
            )

    for node in ast.walk(tree):
        _record_doc(node)

    def _in_docstring(lineno: int) -> bool:
        for start, end in docstring_ranges:
            if start <= lineno <= end:
                return True
        return False

    # Allowed site: `AppSpec = BaseApp` in apps/__init__.py
    # (single ast.Assign line).
    if path == APPS_INIT:
        return []

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "AppSpec":
            if not _in_docstring(node.lineno):
                offenders.append(
                    (node.lineno, "AppSpec reference")
                )
        elif isinstance(node, ast.Attribute) and node.attr == "AppSpec":
            if not _in_docstring(node.lineno):
                offenders.append(
                    (node.lineno, "AppSpec attr reference")
                )
        elif (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "AppSpec" for alias in node.names)
        ):
            if not _in_docstring(node.lineno):
                offenders.append(
                    (
                        node.lineno,
                        f"from {node.module} import AppSpec",
                    )
                )
        elif (
            isinstance(node, ast.Import)
            and any(alias.name == "AppSpec" for alias in node.names)
        ):
            if not _in_docstring(node.lineno):
                offenders.append(
                    (node.lineno, "import AppSpec")
                )

    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    lines = src.splitlines()
    for lineno, snippet in offenders:
        key = (lineno, snippet)
        if key in seen:
            continue
        seen.add(key)
        line_text = lines[lineno - 1].strip()
        out.append((lineno, f"{snippet}: `{line_text}`"))
    return out


def test_apps_no_appspec_refs_outside_alias() -> None:
    """`apps/*.py` MUST NOT reference `AppSpec` outside
    the single allowed alias line in `apps/__init__.py`.
    Every Name / Attribute / Import / ImportFrom that
    touches `AppSpec` should be replaced with `BaseApp`.
    """
    offenders: list[tuple[str, int, str]] = []
    for app_py in sorted(APPS_DIR.glob("*.py")):
        if app_py.name == "__pycache__":
            continue
        for line_no, snippet in _appspec_refs_outside_allowed_site(
            app_py
        ):
            offenders.append((app_py.name, line_no, snippet))
    assert not offenders, (
        f"Forbidden post-WP15 AppSpec reference. The "
        f"`AppSpec` Protocol was removed in WP15; the "
        f"`AppSpec` bare alias lives only on "
        f"`apps/__init__.py`. New code reaches for "
        f"`BaseApp` directly. Offenders: {offenders}"
    )
