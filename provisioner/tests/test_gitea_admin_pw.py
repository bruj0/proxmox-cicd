"""Tests for the Gitea admin-password lifecycle in
``provisioner/lib/apps/gitea.py``.

Three behaviors, one test class each:

  - ``test_read_or_generate_*`` — the /tmp/gitea-admin.pw
    helper generates a fresh strong password on first
    apply, reads it back on subsequent applies, and
    refuses empty files.

  - ``test_dotenv_parsing_*`` — the Vaultwarden master
    password + email + server_url flow from
    ``catalog.yaml`` + ``.env``. Catalog values win over
    .env; .env wins over the canonical hard-coded
    defaults; an empty .env raises a clear error.

  - ``test_skip_admin_seed_flag`` — the catalog's
    ``vaultwarden.skip_admin_seed: true`` short-circuits
    the Vaultwarden Secure Note push without skipping
    the cluster Secret write.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps.gitea import (
    GiteaApp,
)


# ============================================================ read-or-generate


class TestReadOrGenerateAdminPassword:
    """The canonical /tmp/gitea-admin.pw helper is the
    source of truth for the Gitea admin password across
    applies.
    """

    def test_generates_strong_password_on_first_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redirect the helper's hard-coded /tmp path to a
        # tmp_path scratch file so we don't pollute the
        # host's /tmp.
        scratch = tmp_path / "gitea-admin.pw"
        monkeypatch.setattr(
            "provisioner.lib.apps.gitea.ADMIN_PASSWORD_FILE",
            scratch,
        )
        ctx = _make_ctx(tmp_path)
        pw = GiteaApp()._read_or_generate_admin_password(ctx)
        # 32 chars from the unambiguous alphabet.
        assert len(pw) == 32
        assert scratch.exists()
        # File is mode 0600 (owner-only).
        mode = scratch.stat().st_mode & 0o777
        assert mode == 0o600

    def test_returns_existing_password_on_subsequent_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "gitea-admin.pw"
        scratch.write_text("operator-set-strong-password-1234\n")
        scratch.chmod(0o600)
        monkeypatch.setattr(
            "provisioner.lib.apps.gitea.ADMIN_PASSWORD_FILE",
            scratch,
        )
        ctx = _make_ctx(tmp_path)
        pw = GiteaApp()._read_or_generate_admin_password(ctx)
        assert pw == "operator-set-strong-password-1234"

    def test_raises_on_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "gitea-admin.pw"
        scratch.write_text("\n\n")
        monkeypatch.setattr(
            "provisioner.lib.apps.gitea.ADMIN_PASSWORD_FILE",
            scratch,
        )
        ctx = _make_ctx(tmp_path)
        with pytest.raises(RuntimeError, match="is empty"):
            GiteaApp()._read_or_generate_admin_password(ctx)


# ============================================================ .env parsing


class TestDotenvParsing:
    """The Vaultwarden creds flow from catalog.yaml + .env.
    Order of precedence: catalog > .env > canonical defaults.
    """

    def test_missing_master_password_returns_empty(
        self, tmp_path: Path
    ) -> None:
        # Write a .env with everything except the master pw.
        (tmp_path / ".env").write_text(
            "VAULTWARDEN__SERVERURL=https://bitwarden.bruj0.net\n"
            "client_id=user.x\n"
            "client_secret=sec\n"
        )
        creds = GiteaApp._read_dotenv_creds(
            tmp_path, catalog={}
        )
        # master_password is empty — the apply path
        # raises the clear "missing from .env" error
        # when it sees this.
        assert creds["master_password"] == ""
        # server_url still resolves via .env.
        assert creds["server_url"] == "https://bitwarden.bruj0.net"
        # email falls back to the canonical operator
        # account when .env has nothing.
        assert creds["email"] == "secrets@bruj0.net"

    def test_reads_canonical_key(
        self, tmp_path: Path
    ) -> None:
        # The orchestrator's canonical key is
        # VAULTWARDEN__MASTERPASSWORD (matches VKS's own
        # env-var contract). The form with one underscore
        # between VAULT and WARDEN was a workaround for
        # when the .env key happened to render that way;
        # the canonical spelling wins now.
        (tmp_path / ".env").write_text(
            "VAULTWARDEN__MASTERPASSWORD=almasureniam0rd0r\n"
        )
        creds = GiteaApp._read_dotenv_creds(
            tmp_path, catalog={}
        )
        assert creds["master_password"] == "almasureniam0rd0r"

    def test_catalog_overrides_dotenv(
        self, tmp_path: Path
    ) -> None:
        # The catalog is the highest-priority source for
        # the master password. (We don't actually let
        # operators do this in production — the master pw
        # is secret-of-secrets — but the precedence rule
        # is enforced uniformly.)
        (tmp_path / ".env").write_text(
            "VAULTWARDEN__MASTERPASSWORD=from_env\n"
        )
        creds = GiteaApp._read_dotenv_creds(
            tmp_path,
            catalog={"vaultwarden": {"master_password": "from_catalog"}},
        )
        assert creds["master_password"] == "from_catalog"

    def test_server_url_precedence(
        self, tmp_path: Path
    ) -> None:
        # catalog.vaultwarden.server_url wins over
        # VAULTWARDEN__SERVERURL in .env wins over the
        # canonical hard-coded default.
        (tmp_path / ".env").write_text(
            "VAULTWARDEN__SERVERURL=https://from-env.example.com\n"
        )
        # .env only
        creds1 = GiteaApp._read_dotenv_creds(tmp_path, catalog={})
        assert creds1["server_url"] == "https://from-env.example.com"
        # catalog overrides
        creds2 = GiteaApp._read_dotenv_creds(
            tmp_path,
            catalog={"vaultwarden": {"server_url": "https://from-catalog"}},
        )
        assert creds2["server_url"] == "https://from-catalog"
        # no catalog, no .env -> canonical default
        creds3 = GiteaApp._read_dotenv_creds(
            tmp_path / "no_env_here", catalog={}
        )
        assert creds3["server_url"] == "https://bitwarden.bruj0.net"


# ============================================================ skip flag


class TestSkipAdminSeedFlag:
    """``catalog.vaultwarden.skip_admin_seed: true``
    short-circuits the Vaultwarden Secure Note push
    without skipping the cluster Secret write.
    """

    def test_skip_flag_short_circuits_vaultwarden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "gitea-admin.pw"
        scratch.write_text("test-pw-12345678\n")
        monkeypatch.setattr(
            "provisioner.lib.apps.gitea.ADMIN_PASSWORD_FILE",
            scratch,
        )
        # The catalog says skip — the helper must return
        # immediately without calling kubectl.get for the
        # VKS Secret. We assert that by passing a kubectl
        # mock whose .get() raises if called.
        kubectl_mock = MagicMock()
        kubectl_mock.get = MagicMock(
            side_effect=AssertionError(
                "kubectl.get should not be called when "
                "skip_admin_seed=true"
            )
        )
        ctx = _make_ctx(tmp_path, kubectl=kubectl_mock)

        # Should not raise.
        GiteaApp()._seed_admin_password_to_vaultwarden(
            ctx,
            catalog={"vaultwarden": {"skip_admin_seed": True}},
            password="test-pw-12345678",
        )

        # And the kubectl mock was never touched.
        kubectl_mock.get.assert_not_called()

    def test_no_skip_calls_kubectl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When skip is False (default), the helper
        # proceeds past the early return and tries to
        # kubectl.get the VKS Secret. We assert that
        # the helper at minimum calls kubectl.get.
        scratch = tmp_path / "gitea-admin.pw"
        scratch.write_text("test-pw-12345678\n")
        monkeypatch.setattr(
            "provisioner.lib.apps.gitea.ADMIN_PASSWORD_FILE",
            scratch,
        )
        (tmp_path / ".env").write_text(
            "VAULTWARDEN__MASTERPASSWORD=master-from-env\n"
        )
        kubectl_mock = MagicMock()
        # Make kubectl.get return not-found so the helper
        # fails with a clear "VW not installed" error
        # rather than crashing on a base64-decode of None.
        kubectl_mock.get = MagicMock(
            return_value=MagicMock(
                returncode=1, stdout="", stderr="not found"
            )
        )
        ctx = _make_ctx(tmp_path, kubectl=kubectl_mock)

        with pytest.raises(RuntimeError, match="BW_CLIENTID"):
            GiteaApp()._seed_admin_password_to_vaultwarden(
                ctx,
                catalog={},
                password="test-pw-12345678",
            )
        assert kubectl_mock.get.called


# ============================================================ helpers


def _make_ctx(
    repo: Path, kubectl: object | None = None
) -> object:
    """Build a minimal Container for the helper methods.

    The full Container() requires the live
    proxmox-k3s kubeconfig; for these tests we only
    touch the .env reader + the skip flag (no kubectl
    reads, no helm), so a MagicMock is enough.
    """
    ctx = MagicMock()
    ctx.repo_root = repo
    ctx.proxmox_k3s_repo = repo
    if kubectl is not None:
        ctx.kubectl = kubectl
    return ctx
