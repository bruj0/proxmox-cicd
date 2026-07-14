"""Tests for ``VaultwardenClient.list_ciphers`` response-shape handling.

The Bitwarden / Vaultwarden REST API changed ``/api/ciphers``
between releases:

  - Legacy: bare JSON array ``[<cipher>, ...]``.
  - Current (Vaultwarden 1.34.0+ / Bitwarden cloud):
    paginated envelope
    ``{"object": "list", "data": [<cipher>, ...],
    "continuationToken": null}``.

The library must accept both shapes and always return a
flat list of ciphers to callers. A regression that
mistakenly assumes the bare-list shape breaks every
operator who runs ``uv run vaultwarden-notes list`` on
the current Vaultwarden — the error is opaque
("response was not a list; got type dict") and was the
root cause of the 2026-07-14 operator confusion when
7 duplicate Secure Notes were left in the vault after a
broken orchestrator re-seed loop.

What we lock down:

  - Paginated envelope (``object == "list"`` with ``data``
    list) → returns the ``data`` list.
  - Bare array → returns it as-is.
  - Unexpected shape (dict without ``object == "list"``,
    scalar, etc.) → raises ``RuntimeError`` so the bug is
    loud, not silent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from provisioner.lib.vaultwarden.client import VaultwardenClient


def _make_client() -> VaultwardenClient:
    """Build a client with a stub opener; we never make a
    real HTTP call here — the opener is intercepted by the
    per-test patch."""
    return VaultwardenClient(
        server_url="https://bitwarden.example",
        email="secrets@example",
        access_token="fake-token",
        user_key=b"\x00" * 64,
    )


class TestListCiphersResponseShape:
    def test_paginated_envelope_returns_data_list(self, monkeypatch):
        """Vaultwarden 1.34.0+ paginated envelope shape."""
        client = _make_client()
        fake = MagicMock(return_value={
            "object": "list",
            "data": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "continuationToken": None,
        })
        # Patch the helper imported into the client module,
        # not the http module, so the client sees the fake.
        monkeypatch.setattr(
            "provisioner.lib.vaultwarden.client.http_get_json", fake
        )
        result = client.list_ciphers()
        assert result == [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    def test_bare_list_passes_through(self, monkeypatch):
        """Older Vaultwarden bare-array shape."""
        client = _make_client()
        fake = MagicMock(return_value=[{"id": "x"}, {"id": "y"}])
        monkeypatch.setattr(
            "provisioner.lib.vaultwarden.client.http_get_json", fake
        )
        result = client.list_ciphers()
        assert result == [{"id": "x"}, {"id": "y"}]

    def test_unexpected_dict_raises(self, monkeypatch):
        """An error envelope (dict without ``object == list``)
        must surface as RuntimeError, not be silently
        treated as a list."""
        client = _make_client()
        fake = MagicMock(return_value={
            "error": "rate_limited",
            "message": "Too many requests",
        })
        monkeypatch.setattr(
            "provisioner.lib.vaultwarden.client.http_get_json", fake
        )
        import pytest
        with pytest.raises(RuntimeError, match="response was not a list"):
            client.list_ciphers()

    def test_scalar_response_raises(self, monkeypatch):
        client = _make_client()
        fake = MagicMock(return_value="oops")
        monkeypatch.setattr(
            "provisioner.lib.vaultwarden.client.http_get_json", fake
        )
        import pytest
        with pytest.raises(RuntimeError, match="response was not a list"):
            client.list_ciphers()

    def test_passes_query_params(self, monkeypatch):
        """``organization_id`` and ``folder_id`` filter
        params must reach the URL."""
        client = _make_client()
        captured: dict = {}
        def fake(opener, url, token):
            captured["url"] = url
            captured["token"] = token
            return {"object": "list", "data": [], "continuationToken": None}
        monkeypatch.setattr(
            "provisioner.lib.vaultwarden.client.http_get_json", fake
        )
        client.list_ciphers(organization_id="org-1", folder_id="fld-2")
        assert "organizationId=org-1" in captured["url"]
        assert "folderId=fld-2" in captured["url"]
        assert captured["token"] == "fake-token"


class TestDecryptCipherFieldHelpers:
    """Pin the asymmetric semantics of the two field
    helpers — ``decrypt_cipher_field`` returns the VALUE
    (always), ``decrypt_cipher_field_name`` returns the
    NAME (always, by index only). The 2026-07-14 dup-
    finding workflow almost tripped over this: callers
    that pass the result of one into the other as a
    ``name=`` lookup see a ``KeyError`` because no
    field's name decrypts to that value."""

    def test_decrypt_cipher_field_name_returns_name_by_index(self):
        import os
        from provisioner.lib.vaultwarden.crypto import encrypt_str_for_vault
        client = _make_client()
        client.user_key = os.urandom(64)
        cipher = {"fields": [
            {"name": encrypt_str_for_vault("namespaces", client.user_key),
             "value": encrypt_str_for_vault("cloudflared", client.user_key)},
            {"name": encrypt_str_for_vault("secret-name", client.user_key),
             "value": encrypt_str_for_vault("cloudflare-tunnel-remote", client.user_key)},
        ]}
        assert client.decrypt_cipher_field_name(cipher, index=0) == "namespaces"
        assert client.decrypt_cipher_field_name(cipher, index=1) == "secret-name"

    def test_decrypt_cipher_field_name_out_of_range_raises(self):
        client = _make_client()
        cipher = {"fields": []}
        import pytest
        with pytest.raises(IndexError, match="out of range"):
            client.decrypt_cipher_field_name(cipher, index=0)

    def test_decrypt_cipher_field_by_index_returns_value(self):
        import os
        from provisioner.lib.vaultwarden.crypto import encrypt_str_for_vault
        client = _make_client()
        client.user_key = os.urandom(64)
        cipher = {"fields": [
            {"name": encrypt_str_for_vault("namespaces", client.user_key),
             "value": encrypt_str_for_vault("cloudflared", client.user_key)},
            {"name": encrypt_str_for_vault("secret-name", client.user_key),
             "value": encrypt_str_for_vault("cloudflare-tunnel-remote", client.user_key)},
        ]}
        # By index → VALUE (not name).
        assert client.decrypt_cipher_field(cipher, index=0) == "cloudflared"
        assert client.decrypt_cipher_field(cipher, index=1) == "cloudflare-tunnel-remote"

    def test_decrypt_cipher_field_by_name_returns_value(self):
        import os
        from provisioner.lib.vaultwarden.crypto import encrypt_str_for_vault
        client = _make_client()
        client.user_key = os.urandom(64)
        cipher = {"fields": [
            {"name": encrypt_str_for_vault("namespaces", client.user_key),
             "value": encrypt_str_for_vault("cloudflared", client.user_key)},
        ]}
        # By NAME → VALUE (the docstring says "name (the
        # decrypted field name)", and the method matches
        # on decrypted names then returns the value).
        assert client.decrypt_cipher_field(cipher, name="namespaces") == "cloudflared"

    def test_decrypt_cipher_field_by_value_does_not_match_name(self):
        """Pins the asymmetric semantics: passing a VALUE
        as the ``name=`` lookup fails (no field's name
        decrypts to that value)."""
        import os
        from provisioner.lib.vaultwarden.crypto import encrypt_str_for_vault
        client = _make_client()
        client.user_key = os.urandom(64)
        cipher = {"fields": [
            {"name": encrypt_str_for_vault("namespaces", client.user_key),
             "value": encrypt_str_for_vault("cloudflared", client.user_key)},
        ]}
        import pytest
        with pytest.raises(KeyError, match="no cipher field matched"):
            client.decrypt_cipher_field(cipher, name="cloudflared")
