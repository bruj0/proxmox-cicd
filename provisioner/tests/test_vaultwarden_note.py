"""Tests for ``provisioner.lib.vaultwarden.note``.

Verifies the VKS-shaped Secure Note payload that the
orchestrator pushes via ``VaultwardenClient.create_cipher``.

What we lock down:

  - ``vks_triple`` builds the canonical
    ``{namespaces, secret-name, secret-key}`` dict in the
    right order so VKS picks up the right Secret.
  - ``build_secure_note_payload`` returns a dict with the
    right shape (``type=2``, ``secureNote.type=0``, all
    fields encrypted with the user key).
  - The encrypted fields round-trip: decrypting any
    encrypted value with ``decrypt_str_from_vault`` returns
    the original plaintext.
  - Empty ``custom_fields`` is supported (vanilla note).
"""

from __future__ import annotations

import secrets

from provisioner.lib.vaultwarden.crypto import decrypt_str_from_vault
from provisioner.lib.vaultwarden.note import (
    FIELD_TYPE_TEXT,
    SECURE_NOTE_GENERIC,
    TYPE_SECURE_NOTE,
    VKS_FIELD_MAP,
    build_secure_note_payload,
    vks_triple,
)


class TestVksTriple:
    def test_returns_canonical_keys(self):
        triple = vks_triple(
            namespace="cloudflared",
            secret_name="cloudflared-cloudflare-tunnel",
            secret_key="tunnelToken",
        )
        assert triple == {
            "namespaces": "cloudflared",
            "secret-name": "cloudflared-cloudflare-tunnel",
            "secret-key": "tunnelToken",
        }

    def test_uses_documented_field_map(self):
        # The keys must match what VKS reads at the in-cluster
        # Deployment's SYNC__FIELD__* env vars. If these
        # names change in VKS, update VKS_FIELD_MAP too.
        assert set(VKS_FIELD_MAP.values()) == {
            "namespaces",
            "secret-name",
            "secret-key",
        }


class TestBuildSecureNotePayload:
    def _user_key(self) -> bytes:
        return secrets.token_bytes(64)

    def test_basic_shape(self):
        uk = self._user_key()
        payload = build_secure_note_payload(
            note_name="cloudflared k8s secret value",
            body_text="some-secret-value",
            custom_fields=vks_triple(
                namespace="cloudflared",
                secret_name="cloudflare-tunnel-remote",
                secret_key="tunnelToken",
            ),
            user_key=uk,
        )
        assert payload["type"] == TYPE_SECURE_NOTE
        assert payload["secureNote"]["type"] == SECURE_NOTE_GENERIC
        assert payload["favorite"] is False
        assert len(payload["fields"]) == 3
        for f in payload["fields"]:
            assert f["type"] == FIELD_TYPE_TEXT

    def test_all_strings_are_encrypted(self):
        uk = self._user_key()
        body = "the-quick-brown-fox"
        name = "app k8s secret value"
        payload = build_secure_note_payload(
            note_name=name,
            body_text=body,
            custom_fields=vks_triple(
                namespace="ns",
                secret_name="sn",
                secret_key="sk",
            ),
            user_key=uk,
        )
        # Plaintexts must NOT appear directly. (The encrypted
        # ciphertext is base64, so the bytes of the plaintext
        # *could* coincidentally land in a base64 string —
        # the round-trip test below is the load-bearing check.)
        # We assert the plaintexts are not at the start of
        # any envelope: a Type-2 envelope always begins with
        # ``2.``, so if any of our plaintexts is the very first
        # thing in an envelope string, that's a leak.
        for enc in [payload["name"], payload["notes"]] + [
            f["name"] for f in payload["fields"]
        ] + [f["value"] for f in payload["fields"]]:
            assert enc.startswith("2."), f"envelope does not start with 2.: {enc[:30]!r}"
            assert body not in enc
            assert name not in enc
        # And the high-level payload dict doesn't contain the
        # plaintext at the JSON key level (only as encrypted
        # base64 inside the envelope strings, which is fine).
        assert "type" in payload  # sanity — JSON key preserved

    def test_round_trip_each_field(self):
        uk = self._user_key()
        body = "actual-secret-bytes-1234"
        triple = vks_triple(
            namespace="cloudflared",
            secret_name="cloudflare-tunnel-remote",
            secret_key="tunnelToken",
        )
        payload = build_secure_note_payload(
            note_name="cloudflared k8s secret value",
            body_text=body,
            custom_fields=triple,
            user_key=uk,
        )
        # Decrypt every encrypted string back; assert we get
        # exactly what we put in.
        assert decrypt_str_from_vault(payload["name"], uk) == (
            "cloudflared k8s secret value"
        )
        assert decrypt_str_from_vault(payload["notes"], uk) == body
        # The custom fields are stored as a list of {name,
        # value} dicts; iterate and decrypt both sides.
        decrypted = {}
        for f in payload["fields"]:
            k = decrypt_str_from_vault(f["name"], uk)
            v = decrypt_str_from_vault(f["value"], uk)
            decrypted[k] = v
        assert decrypted == triple

    def test_no_custom_fields(self):
        # Vanilla note (no VKS fields) is supported.
        uk = self._user_key()
        payload = build_secure_note_payload(
            note_name="plain note",
            body_text="hello",
            custom_fields=None,
            user_key=uk,
        )
        assert payload["fields"] == []
        assert decrypt_str_from_vault(payload["notes"], uk) == "hello"
