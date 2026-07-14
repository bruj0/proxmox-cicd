"""`provisioner.lib.vaultwarden` — library for talking to a
Vaultwarden (Bitwarden-compatible) REST API from Python.

This package is the refactored spine of the original
`scripts/vaultwarden-seed-note.py`. It exposes the same
crypto + HTTP + auth helpers as importable functions and
classes so the orchestrator and other scripts can reuse them
without re-implementing Bitwarden's PBKDF2 → AES-256-CBC +
HMAC-SHA256 envelope dance.

Module map:

  - `crypto`  — Bitwarden symmetric primitives (PBKDF2
    master key, auth hash, HKDF-Expand (enc, mac) key
    stretch, AES-256-CBC encrypt/decrypt, envelope
    parse + format).
  - `http`    — opener + headers Cloudflare/Vaultwarden
    require (`User-Agent`, `Bitwarden-Client-Version`,
    `device-type`); JSON + form POST + GET + DELETE
    helpers that surface non-2xx as `VaultwardenHTTPError`.
  - `client`  — `VaultwardenClient`: login, profile fetch,
    cipher list, name/field decryption, create + delete.
  - `note`    — `build_secure_note_payload` + the VKS-
    specific cipher field triple (`namespaces`,
    `secret-name`, `secret-key`).

The CLI lives at `scripts/vaultwarden-notes.py` and exposes
subcommands `seed`, `delete`, `list`, `decrypt`.
"""

from __future__ import annotations

from provisioner.lib.vaultwarden.crypto import (
    DEFAULT_KDF_ITERATIONS,
    ENC_TYPE,
    aes_cbc_encrypt,
    b64,
    decrypt_str_from_vault,
    encrypt_str_for_vault,
    make_master_key,
    make_server_auth_hash,
    split_user_key,
    stretch_master_key,
    unwrap_user_key,
)
from provisioner.lib.vaultwarden.http import (
    DEFAULT_CLIENT_VERSION,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_USER_AGENT,
    VaultwardenHTTPError,
    build_opener,
    http_delete,
    http_get_json,
    http_post_form,
    http_post_json,
)
from provisioner.lib.vaultwarden.client import VaultwardenClient
from provisioner.lib.vaultwarden.note import (
    FIELD_TYPE_TEXT,
    SECURE_NOTE_GENERIC,
    TYPE_SECURE_NOTE,
    VKS_FIELD_MAP,
    build_secure_note_payload,
    vks_triple,
)

__all__ = [
    # crypto
    "DEFAULT_KDF_ITERATIONS",
    "ENC_TYPE",
    "aes_cbc_encrypt",
    "b64",
    "decrypt_str_from_vault",
    "encrypt_str_for_vault",
    "make_master_key",
    "make_server_auth_hash",
    "split_user_key",
    "stretch_master_key",
    "unwrap_user_key",
    # http
    "DEFAULT_CLIENT_VERSION",
    "DEFAULT_DEVICE_TYPE",
    "DEFAULT_USER_AGENT",
    "VaultwardenHTTPError",
    "build_opener",
    "http_delete",
    "http_get_json",
    "http_post_form",
    "http_post_json",
    # client + note
    "VaultwardenClient",
    "FIELD_TYPE_TEXT",
    "SECURE_NOTE_GENERIC",
    "TYPE_SECURE_NOTE",
    "VKS_FIELD_MAP",
    "build_secure_note_payload",
    "vks_triple",
]
