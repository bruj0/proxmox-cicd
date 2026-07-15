"""test_apps_no_inline_vaultwarden_client — WP12 static guard.

WP12 ships a canonical Vaultwarden client lifecycle on
`BaseApp`:

  * `BaseApp._read_dotenv_creds(repo_root, catalog)`
  * `BaseApp._vaultwarden_client(ctx, catalog)`
  * `BaseApp._seed_vaultwarden_note(...)`

After WP12, those helpers are the only allowed
consumers of `provisioner.lib.vaultwarden.VaultwardenClient`.
A future contributor who bypasses the helpers (e.g.
by importing `VaultwardenClient` directly into an
app module) drifts from the canonical contract.

This test scans `apps/*.py` for direct imports of
`VaultwardenClient` and fails on any hit. Apps
reach for `BaseApp._vaultwarden_client(...)` (or
`BaseApp._read_dotenv_creds(...)`) instead.

Companion tests:

  * `test_apps_no_inline_wp9_patterns.py` (WP9) —
    forbids inline `SecretKeySelector`, hardcoded
    `chart_version`, etc.
  * `test_no_alt_render_layer.py` (WP10) — forbids
    inline `yaml.safe_dump` / `yaml.dump` /
    `yaml.safe_load` in app modules.
"""

from __future__ import annotations

import ast
from pathlib import Path


PROVISIONER = Path(__file__).resolve().parents[2]
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"


def _app_py_files() -> list[Path]:
    """Every `apps/*.py` except `__init__.py` and
    `base.py` (the canonical helpers live on `BaseApp`).
    """
    return sorted(
        path
        for path in APPS_DIR.glob("*.py")
        if path.name not in ("__init__.py", "base.py")
    )


def _vaultwarden_client_imports_in_code(path: Path) -> list[tuple[int, str]]:
    """Use AST to find imports / attribute references
    of `VaultwardenClient` outside docstrings + comments.
    Returns `[(line, snippet)]` for every offender.
    """
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    # Collect line ranges that are docstrings (Expressions
    # whose value is a Constant string at module or
    # function/class level). Code outside those ranges
    # is "the program" — only there do we forbid the
    # literal name.
    docstring_ranges: list[tuple[int, int]] = []

    # Module-level docstring.
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

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "VaultwardenClient":
                    if not _in_docstring(node.lineno):
                        offenders.append(
                            (
                                node.lineno,
                                f"from {node.module} import "
                                "VaultwardenClient",
                            )
                        )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "VaultwardenClient":
                    if not _in_docstring(node.lineno):
                        offenders.append(
                            (
                                node.lineno,
                                "import VaultwardenClient",
                            )
                        )
        elif isinstance(node, ast.Name) and node.id == "VaultwardenClient":
            if not _in_docstring(node.lineno):
                offenders.append(
                    (node.lineno, "VaultwardenClient reference")
                )
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "VaultwardenClient"
        ):
            if not _in_docstring(node.lineno):
                offenders.append(
                    (node.lineno, "VaultwardenClient attr reference")
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


def test_apps_do_not_import_vaultwarden_client_directly() -> None:
    """`apps/*.py` MUST NOT import
    `provisioner.lib.vaultwarden.VaultwardenClient`
    directly. All Vaultwarden-client operations must
    route through `BaseApp._vaultwarden_client` (which
    is the only consumer of the library in
    `apps/base.py`).
    """
    offenders: list[tuple[str, int, str]] = []
    for app_py in _app_py_files():
        for line_no, snippet in _vaultwarden_client_imports_in_code(
            app_py
        ):
            offenders.append((app_py.name, line_no, snippet))
    assert not offenders, (
        f"Forbidden post-WP12 direct VaultwardenClient "
        f"import. Apps must route through "
        f"`BaseApp._vaultwarden_client(...)` (which reads "
        f"BW_CLIENTID/BW_CLIENTSECRET + .env creds and "
        f"performs login) — see `BaseApp._seed_vaultwarden_note` "
        f"for the canonical write path. Offenders: {offenders}"
    )
