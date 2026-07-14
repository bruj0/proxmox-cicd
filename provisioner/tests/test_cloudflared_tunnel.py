"""Tests for `provisioner.lib.apps.cloudflared_tunnel`.

What we cover:
  - `TunnelRecord` carries the canonical fields
    (`id`, `name`, `token`, `credentials_file`).
  - `CloudflaredTunnelClient.mint` reads the **base64 tunnel
    token** from `result.token` (the contract Cloudflare ships
    in 2024–2026 for `config_src=cloudflare` tunnels), and
    populates both `record.token` (base64 string) and
    `record.credentials_file` (decoded dict).
  - `CloudflaredTunnelClient.mint` falls back to `result.
    credentials_file` when only the decoded dict is present,
    and re-encodes it to the canonical base64 form so
    cloudflared sees the same input either way.
  - `CloudflaredTunnelClient.list_by_name` filters tunnels.
  - `CloudflaredTunnelClient.delete` 1-arg delete works.
  - `CloudflaredTunnelClient.rotate` deletes then mints under
    the same name and returns the new `TunnelRecord` with
    its base64 token populated.
  - `persist` writes mode-0600 JSON with `tunnel_token`
    as the base64 string and `credentials_file` as the
    decoded dict.
  - End-to-end happy path: the orchestrator's `_ensure_tunnel`
    reuses a valid cached base64 token (idempotent), rotates a
    corrupt (compact-JSON) cached record, and mints fresh when
    there's no cache.
"""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from provisioner.lib.apps.cloudflared_tunnel import (
    CloudflaredTunnelClient,
    TunnelRecord,
    _CfError,
    decode_credentials_blob,
    persist as persist_tunnel,
)


# ---------- fixtures ----------

# A real-shaped base64 tunnel token. The decoded compact
# JSON has the canonical `{a, t, s}` keys cloudflared reads.
TUNNEL_ID = "cbaa8807-d359-41d6-bab9-19062e75274c"
ACCOUNT_TAG = "2e9c09b27d2a089c531b12ae0f0e6ff3"
TUNNEL_NAME = "cicd-tunnel"
TUNNEL_SECRET = "O8mazVLLc9b+IcIeZRVO1dqdJBb64hnit0rOsNfT8j..."

# Build a canonical base64 token the same way the API does
# (compact JSON, base64-encoded).
BASE64_TOKEN = base64.b64encode(
    json.dumps(
        {"a": ACCOUNT_TAG, "t": TUNNEL_ID, "s": TUNNEL_SECRET},
        separators=(",", ":"),
    ).encode("utf-8")
).decode("ascii")


def _post_response(
    *,
    tunnel_id: str = TUNNEL_ID,
    name: str = TUNNEL_NAME,
    token: str = BASE64_TOKEN,
    secret: str = TUNNEL_SECRET,
    account_tag: str = ACCOUNT_TAG,
) -> dict[str, Any]:
    """Build the **unwrapped inner payload** for POST
    /cfd_tunnel. The real `_cf_request` strips the
    `{success, errors, messages, result}` envelope and
    returns `result` directly; tests should mock with
    the already-unwrapped shape so the production
    assertions stay simple.
    """
    return {
        "id": tunnel_id,
        "account_tag": account_tag,
        "created_at": "2026-01-01T00:00:00Z",
        "deleted_at": None,
        "name": name,
        "connections": [],
        "conns_active_at": None,
        "conns_inactive_at": "2026-01-01T00:00:00Z",
        "tun_type": "cfd_tunnel",
        "metadata": {},
        "status": "inactive",
        "remote_config": True,
        "config_src": "cloudflare",
        "credentials_file": {
            "AccountTag": account_tag,
            "TunnelID": tunnel_id,
            "TunnelName": name,
            "TunnelSecret": secret,
        },
        "token": token,
    }


def _post_response_no_token(
    *,
    tunnel_id: str = TUNNEL_ID,
    name: str = TUNNEL_NAME,
    secret: str = TUNNEL_SECRET,
    account_tag: str = ACCOUNT_TAG,
) -> dict[str, Any]:
    """POST /cfd_tunnel payload with **no `token` field** —
    exercises the `credentials_file`-only fallback path."""
    return {
        "id": tunnel_id,
        "name": name,
        "config_src": "cloudflare",
        "credentials_file": {
            "AccountTag": account_tag,
            "TunnelID": tunnel_id,
            "TunnelName": name,
            "TunnelSecret": secret,
        },
    }


def _list_response(name: str = TUNNEL_NAME, tunnel_id: str | None = None) -> list[dict[str, Any]]:
    """Build a fake API response for GET /cfd_tunnel (list).
    The list_by_name client unwraps `result` from the API
    envelope; `_cf_request` does that for us, so this fixture
    returns the inner payload directly.
    """
    return [{"id": tunnel_id, "name": name}] if tunnel_id else []


# ---------- decode_credentials_blob ----------

class TestDecodeCredentialsBlob:
    def test_base64_string_round_trip(self):
        compact = {"a": "acct", "t": "tid", "s": "secret"}
        b64 = base64.b64encode(
            json.dumps(compact, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        out = decode_credentials_blob(b64)
        assert out["AccountTag"] == "acct"
        assert out["TunnelID"] == "tid"
        # TunnelName is read from the optional `name` field —
        # the base64 form doesn't carry it (the dict form does),
        # so this stays empty.
        assert out["TunnelName"] == ""
        assert out["TunnelSecret"] == "secret"

    def test_base64_string_with_name_round_trip(self):
        compact = {"a": "acct", "t": "tid", "s": "secret", "name": "tn"}
        b64 = base64.b64encode(
            json.dumps(compact, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        out = decode_credentials_blob(b64)
        assert out["TunnelName"] == "tn"

    def test_dict_input(self):
        out = decode_credentials_blob(
            {
                "AccountTag": "acct",
                "TunnelID": "abc-123",
                "TunnelName": "name",
                "TunnelSecret": "secret",
            }
        )
        assert out["AccountTag"] == "acct"
        # `decode_credentials_blob` strips dashes from TunnelID
        # to match the compact-JSON `t` convention (Cloudflare's
        # canonical form). The orchestrator restores the dashed
        # UUID after calling this function; tests that go
        # through `mint()` see the dashed form.
        assert out["TunnelID"] == "abc123"
        assert out["TunnelName"] == "name"
        assert out["TunnelSecret"] == "secret"


# ---------- CloudflaredTunnelClient.mint ----------

class TestCloudflaredTunnelClientMint:
    def setup_method(self):
        self.client = CloudflaredTunnelClient(token_value="Bearer-test")
        self.acc = "2e9c09b27d2a089c531b12ae0f0e6ff3"

    def test_mint_returns_tunnelrecord_with_base64_token(self):
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value=_post_response(),
        ) as cf_call:
            record = self.client.mint(self.acc, "cicd-tunnel")

        cf_call.assert_called_once()
        args, kwargs = cf_call.call_args
        assert args[0] == "POST"
        assert args[1] == f"/accounts/{self.acc}/cfd_tunnel"
        assert kwargs["body"]["name"] == "cicd-tunnel"
        assert kwargs["body"]["config_src"] == "cloudflare"
        assert kwargs["token_value"] == "Bearer-test"

        assert isinstance(record, TunnelRecord)
        assert record.id == TUNNEL_ID
        assert record.name == TUNNEL_NAME
        # The token is the **base64 string** verbatim — what
        # cloudflared reads from `$TUNNEL_TOKEN`.
        assert record.token == BASE64_TOKEN
        assert record.token.startswith("eyJ")  # base64 of `{`
        # And credentials_file is the decoded dict.
        assert "TunnelSecret" in record.credentials_file
        assert (
            record.credentials_file["TunnelID"]
            == "cbaa8807-d359-41d6-bab9-19062e75274c"
        )

    def test_mint_falls_back_to_credentials_file_when_token_missing(self):
        """If Cloudflare ships only the decoded dict (older API
        versions), `mint` should re-encode it to the canonical
        base64 form so cloudflared gets the same input."""
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value=_post_response_no_token(),
        ):
            record = self.client.mint(self.acc, "cicd-tunnel")
        # Re-encoded compact JSON, base64 of it.
        assert record.token.startswith("eyJ")
        # Round-trip through the decoder: should give us back
        # the canonical dict (TunnelID without dashes — that's
        # what `decode_credentials_blob` produces; the orchestrator
        # restores the dashed form via `record.credentials_file`).
        decoded = decode_credentials_blob(record.token)
        assert decoded["AccountTag"] == ACCOUNT_TAG
        assert decoded["TunnelID"] == TUNNEL_ID.replace("-", "")
        assert decoded["TunnelSecret"] == TUNNEL_SECRET
        # `mint()` patches the dashed form back into the
        # credentials_file dict so the chart's per-tunnel filename
        # `/etc/cloudflared/<UUID>.json` is unambiguous.
        assert record.credentials_file["TunnelID"] == TUNNEL_ID

    def test_mint_rejects_response_missing_both_fields(self):
        bad = _post_response()
        bad.pop("token", None)
        bad.pop("credentials_file", None)
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value=bad,
        ):
            with pytest.raises(_CfError):
                self.client.mint(self.acc, "cicd-tunnel")


class TestCloudflaredTunnelClientListByName:
    def setup_method(self):
        self.client = CloudflaredTunnelClient(token_value="t")
        self.acc = "acc-id"

    def test_list_returns_tunnels(self):
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value=_list_response(tunnel_id="abc-123"),
        ) as cf_call:
            res = self.client.list_by_name(self.acc, "cicd-tunnel")
        assert res == [{"id": "abc-123", "name": "cicd-tunnel"}]
        _, kwargs = cf_call.call_args
        assert kwargs["query"]["name"] == "cicd-tunnel"
        assert kwargs["query"]["is_deleted"] == "false"


class TestCloudflaredTunnelClientDelete:
    def setup_method(self):
        self.client = CloudflaredTunnelClient(token_value="t")
        self.acc = "acc-id"

    def test_delete_calls_https(self):
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value={"success": True},
        ) as cf_call:
            self.client.delete(self.acc, "abc-123")
        cf_call.assert_called_once()
        args, kwargs = cf_call.call_args
        assert args[0] == "DELETE"
        assert args[1] == f"/accounts/{self.acc}/cfd_tunnel/abc-123"

    def test_delete_propagates_failure(self):
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            return_value={"success": False, "errors": ["x"]},
        ):
            with pytest.raises(_CfError):
                self.client.delete(self.acc, "abc-123")


class TestCloudflaredTunnelClientRotate:
    def setup_method(self):
        self.client = CloudflaredTunnelClient(token_value="t")
        self.acc = "acc-id"

    def test_rotate_deletes_then_mints_under_same_name(self):
        with patch(
            "provisioner.lib.apps.cloudflared_tunnel._cf_request",
            side_effect=[
                {"success": True},
                _post_response(tunnel_id="new-id-456"),
            ],
        ) as cf_call:
            record = self.client.rotate(
                account_id=self.acc,
                tunnel_id="old-id-123",
                tunnel_name="cicd-tunnel",
            )
        assert cf_call.call_count == 2
        assert cf_call.call_args_list[0].args[0] == "DELETE"
        assert "old-id-123" in cf_call.call_args_list[0].args[1]
        assert cf_call.call_args_list[1].args[0] == "POST"
        assert (
            cf_call.call_args_list[1].kwargs["body"]["name"]
            == "cicd-tunnel"
        )
        assert record.id == "new-id-456"
        # The new record also carries the base64 token.
        assert record.token.startswith("eyJ")


# ---------- persist ----------

class TestPersist:
    def test_persist_writes_base64_token_mode_0600(self, tmp_path: Path):
        path = tmp_path / "test-persist-tunnel.json"
        record = TunnelRecord(
            id="abc-123",
            name="cicd-tunnel",
            token=BASE64_TOKEN,
            credentials_file={
                "AccountTag": "x",
                "TunnelID": "abc-123",
                "TunnelName": "cicd-tunnel",
                "TunnelSecret": "s",
            },
        )
        persist_tunnel(record, path)

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["id"] == "abc-123"
        assert data["name"] == "cicd-tunnel"
        assert data["tunnel_token"] == BASE64_TOKEN
        # `tunnel_token` is the base64 string (NOT the
        # decoded compact JSON).
        assert data["tunnel_token"].startswith("eyJ")
        assert "credentials_file" in data
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


# ---------- end-to-end through CloudflaredApp._ensure_tunnel ----------

class TestEnsureTunnelOrchestrator:
    """Drive CloudflaredApp._ensure_tunnel through mocked
    Cloudflare API + filesystem, no live cluster needed."""

    def _app(self):
        from provisioner.lib.apps.cloudflared import CloudflaredApp

        return CloudflaredApp()

    def _ctx(self, repo_root: Path) -> MagicMock:
        ctx = MagicMock()
        ctx.repo_root = repo_root
        ctx.logger = MagicMock()
        return ctx

    def test_idempotent_re_run_uses_cached_base64_token(
        self, tmp_path: Path
    ):
        cached = {
            "id": "abc-123",
            "name": "cicd-tunnel",
            "tunnel_token": BASE64_TOKEN,
            "credentials_file": {
                "AccountTag": "x",
                "TunnelID": "abc-123",
                "TunnelName": "cicd-tunnel",
                "TunnelSecret": "s",
            },
        }
        (tmp_path / "infra/secrets").mkdir(parents=True)
        (tmp_path / "infra/secrets/cloudflared-tunnel.json").write_text(
            json.dumps(cached) + "\n"
        )

        ctx = self._ctx(tmp_path)
        app = self._app()

        with patch(
            "provisioner.lib.apps.cloudflared.CloudflaredTunnelClient.list_by_name",
            return_value=[{"id": "abc-123", "name": "cicd-tunnel"}],
        ) as lc:
            tunnel = app._ensure_tunnel(
                ctx, account_id="acc", token_value="t"
            )
        lc.assert_called_once()
        assert tunnel["id"] == "abc-123"
        assert tunnel["tunnel_token"] == BASE64_TOKEN

    def test_rotates_when_cached_tunnel_token_is_compact_json(
        self, tmp_path: Path
    ):
        """The pre-fix bug stored the *decoded* compact JSON
        in `tunnel_token`. A re-run must detect that (it
        doesn't start with `eyJ`) and rotate the tunnel."""
        corrupt = {
            "id": "abc-123",
            "name": "cicd-tunnel",
            "tunnel_token": json.dumps({"a": "x", "t": "y", "s": "z"}),
            "credentials_file": {
                "AccountTag": "x",
                "TunnelID": "abc-123",
                "TunnelName": "cicd-tunnel",
                "TunnelSecret": "s",
            },
        }
        (tmp_path / "infra/secrets").mkdir(parents=True)
        (tmp_path / "infra/secrets/cloudflared-tunnel.json").write_text(
            json.dumps(corrupt) + "\n"
        )

        ctx = self._ctx(tmp_path)
        app = self._app()

        with patch(
            "provisioner.lib.apps.cloudflared.CloudflaredTunnelClient.list_by_name",
            return_value=[{"id": "abc-123", "name": "cicd-tunnel"}],
        ):
            with patch(
                "provisioner.lib.apps.cloudflared.CloudflaredTunnelClient.rotate",
                return_value=TunnelRecord(
                    id="new-id-456",
                    name="cicd-tunnel",
                    token=BASE64_TOKEN,
                    credentials_file={
                        "AccountTag": "x",
                        "TunnelID": "new-id-456",
                        "TunnelName": "cicd-tunnel",
                        "TunnelSecret": "s2",
                    },
                ),
            ) as rotate:
                tunnel = app._ensure_tunnel(
                    ctx, account_id="acc", token_value="t"
                )
        rotate.assert_called_once()
        _, kwargs = rotate.call_args
        assert kwargs["account_id"] == "acc"
        assert kwargs["tunnel_id"] == "abc-123"
        assert kwargs["tunnel_name"] == "cicd-tunnel"
        assert tunnel["id"] == "new-id-456"
        # Re-issued token is the canonical base64 string.
        assert tunnel["tunnel_token"] == BASE64_TOKEN
        assert tunnel["tunnel_token"].startswith("eyJ")

        on_disk = json.loads(
            (tmp_path / "infra/secrets/cloudflared-tunnel.json").read_text()
        )
        assert on_disk["id"] == "new-id-456"
        assert on_disk["tunnel_token"] == BASE64_TOKEN
        assert on_disk["tunnel_token"].startswith("eyJ")

    def test_mints_fresh_when_no_cache(self, tmp_path: Path):
        (tmp_path / "infra/secrets").mkdir(parents=True)

        ctx = self._ctx(tmp_path)
        app = self._app()

        with patch(
            "provisioner.lib.apps.cloudflared.CloudflaredTunnelClient.list_by_name",
            return_value=[],
        ):
            with patch(
                "provisioner.lib.apps.cloudflared.CloudflaredTunnelClient.mint",
                return_value=TunnelRecord(
                    id="fresh-id",
                    name="cicd-tunnel",
                    token=BASE64_TOKEN,
                    credentials_file={
                        "AccountTag": "x",
                        "TunnelID": "fresh-id",
                        "TunnelName": "cicd-tunnel",
                        "TunnelSecret": "s3",
                    },
                ),
            ) as mint:
                tunnel = app._ensure_tunnel(
                    ctx, account_id="acc", token_value="t"
                )
        mint.assert_called_once_with(
            account_id="acc", tunnel_name="cicd-tunnel"
        )
        assert tunnel["id"] == "fresh-id"
        assert tunnel["tunnel_token"] == BASE64_TOKEN
        assert tunnel["tunnel_token"].startswith("eyJ")
