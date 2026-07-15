"""test_template_rendering — WP5 regression tests for
`_render_template`.

WP5 of the GroupSpec plan moves YAML snippets out of
`apps/*.py` into real files under
`apps/templates/<app>/<name>.yaml`, rendered by
`BaseApp._render_template(name, **vars)` using
`string.Template.safe_substitute`.

These tests pin four invariants on `_render_template`:

  1. Substitution: variables in the template are
     replaced with the kwargs.
  2. Missing-file: a non-existent template raises
     `TemplateNotFoundError` naming the app + path.
  3. Unrendered-variable: a `${var}` referenced in
     the template but not in kwargs raises `KeyError`.
  4. Unused-vars: vars passed but not referenced in
     the template are silently ignored (safe_substitute
     semantics).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provisioner.lib.apps.base import BaseApp, TemplateNotFoundError


TEMPLATES_DIR = Path("provisioner/lib/apps/templates")


class _FixtureApp(BaseApp):
    """A `BaseApp` subclass that points its template
    directory at the on-disk fixture dir so tests can
    exercise the file-rendering path without
    registering a real shipped app.
    """

    name = "_fixture"
    _template_dir_override = TEMPLATES_DIR

    @property
    def template_dir(self) -> Path:
        return self._template_dir_override

    @property
    def nodes(self):  # type: ignore[override]
        return {}

    @property
    def edges(self):  # type: ignore[override]
        return {}

    def enabled_in(self, catalog):  # type: ignore[override]
        return True

    def plan(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def apply(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def destroy(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError

    def status(self, ctx, catalog):  # type: ignore[override]
        raise NotImplementedError


def _write_template(
    path: Path,
    content: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_render_template_substitutes_variables(tmp_path: Path) -> None:
    """`_render_template` reads the template file and
    substitutes `$var` / `${var}` placeholders with the
    kwargs. This is the happy path: caller passes the
    vars the template declares.
    """
    template = _write_template(
        tmp_path / "hello.yaml",
        "app: $app\nhost: ${host}\n",
    )
    app = _FixtureApp()
    app._template_dir_override = tmp_path

    rendered = app._render_template(
        "hello.yaml", app="gitea", host="gitea.example.net"
    )
    assert rendered == (
        "app: gitea\nhost: gitea.example.net\n"
    )
    # The template file path used:
    assert template.exists()


def test_render_template_raises_on_missing_file(tmp_path: Path) -> None:
    """A missing template file raises
    `TemplateNotFoundError` with the app name + the
    relative path so the operator can find it.
    """
    app = _FixtureApp()
    app._template_dir_override = tmp_path

    with pytest.raises(
        TemplateNotFoundError, match="missing.yaml"
    ) as ei:
        app._render_template("missing.yaml", foo="bar")
    # The message names the app name so a future
    # contributor can grep for the right template.
    assert "_fixture" in str(ei.value)


def test_render_template_raises_on_unrendered_var(tmp_path: Path) -> None:
    """A `${var}` referenced in the template but not
    in kwargs raises `KeyError`. `safe_substitute`
    silently leaves the variable unsubstituted, which
    would ship invalid YAML to kubectl — so we wrap
    it: re-scan the rendered string, raise if any
    `$var` survives.
    """
    _write_template(
        tmp_path / "needs-var.yaml",
        "app: $app\nhost: $host\n",
    )
    app = _FixtureApp()
    app._template_dir_override = tmp_path

    # `host` not provided -> KeyError after rendering.
    with pytest.raises(KeyError, match="host"):
        app._render_template("needs-var.yaml", app="gitea")


def test_render_template_falls_back_for_unused_vars(tmp_path: Path) -> None:
    """Vars passed in kwargs but NOT referenced in the
    template are silently dropped. The point of
    `_render_template` is to let callers share a
    single `**catalog` kwargs dict across many
    templates — unused keys must not raise.
    """
    _write_template(
        tmp_path / "one-var.yaml",
        "app: $app\n",
    )
    app = _FixtureApp()
    app._template_dir_override = tmp_path

    rendered = app._render_template(
        "one-var.yaml",
        app="gitea",
        host="ignored",
        port=9999,
        extra={"unused": True},
    )
    assert rendered == "app: gitea\n"


def test_render_template_handles_dollar_literal(tmp_path: Path) -> None:
    """A `$$` in the template renders as a literal `$`
    so YAML values that contain currency / regex
    characters don't trip up the substitution.
    """
    _write_template(
        tmp_path / "literal.yaml",
        "value: $cost_dollars\nliteral: $$\n",
    )
    app = _FixtureApp()
    app._template_dir_override = tmp_path

    rendered = app._render_template(
        "literal.yaml", cost_dollars="free"
    )
    assert "value: free" in rendered
    assert "literal: $" in rendered
