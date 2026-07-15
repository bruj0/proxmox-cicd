"""test_base_app_dotenv — WP11 contract tests for the centralized
`_load_dotenv` and `_require_env` helpers on `BaseApp`.

WP11 lifts the `.env` parser from cloudflared +
vaultwarden_k8s_sync + gitea (which all read the
same `.env` file) onto `BaseApp` so each app can call
`self._load_dotenv(repo_root)` and get a
`dict[str, str]` back. Two callers already delegate
to the VKS module's static `_load_dotenv`; the WP11
canonical parser must be the most permissive of the
three existing implementations (escaped `#` mid-line
stays inside a quoted value; `export FOO=bar` is
stripped; comments and blanks are skipped).

The tests below describe the WP11 contract.
Implementation in `apps/base.py` is the GREEN phase.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provisioner.lib.apps.base import BaseApp


class _ProbeApp(BaseApp):
    """Stand-in subclass that disables the 4 abstract
    methods so we can instantiate `BaseApp` for these
    helper tests."""

    name = "probe"

    def plan(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def apply(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def destroy(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def status(self, ctx, catalog):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def app() -> BaseApp:
    return _ProbeApp()


def test_load_dotenv_parses_simple_keyvalue(
    tmp_path: Path, app: BaseApp
) -> None:
    """A single `KEY=value` line returns a one-entry dict."""
    (tmp_path / ".env").write_text("FOO=bar\n")
    env = app._load_dotenv(tmp_path)
    assert env == {"FOO": "bar"}


def test_load_dotenv_skips_comments_and_blanks(
    tmp_path: Path, app: BaseApp
) -> None:
    """Blank lines and `#` comments are silently dropped."""
    (tmp_path / ".env").write_text(
        "\n"
        "# leading comment\n"
        "\n"
        "FOO=bar\n"
        "   \n"
        "# trailing comment\n"
    )
    env = app._load_dotenv(tmp_path)
    assert env == {"FOO": "bar"}


def test_load_dotenv_handles_quoted_values(
    tmp_path: Path, app: BaseApp
) -> None:
    """Both double- and single-quoted values have the
    surrounding quotes stripped."""
    (tmp_path / ".env").write_text(
        'DOUBLE="hello"\n'
        "SINGLE='world'\n"
        "BARE=plain\n"
    )
    env = app._load_dotenv(tmp_path)
    assert env == {"DOUBLE": "hello", "SINGLE": "world", "BARE": "plain"}


def test_load_dotenv_strips_export_prefix(
    tmp_path: Path, app: BaseApp
) -> None:
    """Lines beginning `export FOO=bar` parse as `FOO=bar`.

    POSIX shells allow `export` on assignment lines and
    the existing CLI helpers sometimes embed generated
    fragments in `~/.bashrc`-style files that picked
    up an `export`. WP11 picks the most permissive
    behaviour from the three pre-WP11 parsers.
    """
    (tmp_path / ".env").write_text("export FOO=bar\n")
    env = app._load_dotenv(tmp_path)
    assert env == {"FOO": "bar"}


def test_load_dotenv_keeps_hash_inside_quoted_value(
    tmp_path: Path, app: BaseApp
) -> None:
    """A `#` inside a quoted value is part of the value,
    not a comment marker. The unquoted `#` mid-line is
    preserved as-is (the WP11 parser does NOT strip
    mid-line `#` — that matches cloudflared's pre-WP11
    behaviour, which is the most permissive of the
    three pre-WP11 parsers)."""
    (tmp_path / ".env").write_text(
        'PASSWORD="abc#def"\n'  # hash inside the quoted value
        "NOTE=value#trailing\n"  # unquoted hash kept verbatim
        "AFTER=clean\n"
    )
    env = app._load_dotenv(tmp_path)
    assert env["PASSWORD"] == "abc#def"
    assert env["NOTE"] == "value#trailing"
    assert env["AFTER"] == "clean"


def test_load_dotenv_returns_empty_when_file_missing(
    tmp_path: Path, app: BaseApp
) -> None:
    """Missing `.env` is not an error — apps that
    have no secrets to read (test paths, dry-runs
    against a fresh checkout) get an empty dict."""
    env = app._load_dotenv(tmp_path)
    assert env == {}


def test_require_env_raises_with_clear_message() -> None:
    """The missing-key error names the missing key so a
    misconfigured operator can grep the audit log.

    WP11 keeps the helper a `@staticmethod` (the
    pre-WP11 callers — `CloudflareApp._require_env`
    in tests, the cross-app `_read_dotenv_creds`
    helpers — call it as a static). The static form
    does not include the app name; that lands in the
    audit log via the calling app's existing log
    machinery, not the exception message."""
    with pytest.raises(RuntimeError) as exc_info:
        BaseApp._require_env({}, "CLOUDFLARE_API_TOKEN")
    msg = str(exc_info.value)
    assert "CLOUDFLARE_API_TOKEN" in msg


def test_require_env_returns_value_when_present() -> None:
    env = {"FOO": "bar"}
    assert BaseApp._require_env(env, "FOO") == "bar"
