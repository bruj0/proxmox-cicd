"""Secure-Note payload builders + VaultwardenK8sSync constants.

VaultwardenK8sSync (VKS) consumes a very specific shape of
cipher: a Secure Note (``type=2``) whose custom fields are
exactly three name/value pairs:

  - ``namespaces``  → k8s namespace VKS will write the Secret into
  - ``secret-name`` → k8s Secret name VKS will create/update
  - ``secret-key``  → key inside the Secret whose value the
                       note body holds

The note body itself is the raw value VKS writes into the
Secret key.

This module bakes those constants in one place and exposes
``build_secure_note_payload`` so the orchestrator (or the
``scripts/vaultwarden-notes.py seed`` CLI) doesn't have to
re-state them every time.
"""

from __future__ import annotations

from typing import Final

from provisioner.lib.vaultwarden.crypto import encrypt_str_for_vault


# Bitwarden cipher `type` discriminator. Secure Note = 2.
TYPE_SECURE_NOTE: Final = 2

# `secureNote.type` inner discriminator. 0 = "generic"
# (no type-specific body). All VKS notes are generic.
SECURE_NOTE_GENERIC: Final = 0

# Custom field `type` discriminator. 0 = text (the only kind
# VKS reads). 1 = hidden (treated as password by the UI),
# 2 = boolean — we don't use these.
FIELD_TYPE_TEXT: Final = 0


#: Map from the VKS field name (as documented in VKS's
#: SYNC__FIELD__* env vars) to a human-friendly default.
#: Keys match the values of the env vars the in-cluster
#: VKS Deployment uses:
#:     SYNC__FIELD__NAMESPACES  = "namespaces"
#:     SYNC__FIELD__SECRETNAME  = "secret-name"
#:     SYNC__FIELD__SECRETKEY   = "secret-key"
#:
#: Override these via env vars at VKS deploy-time if you
#: want a different field naming convention; the orchestrator
#: uses these defaults to match the deployed VKS.
VKS_FIELD_MAP: Final = {
    "namespaces": "namespaces",
    "secret-name": "secret-name",
    "secret-key": "secret-key",
}


def build_secure_note_payload(
    note_name: str,
    body_text: str,
    *,
    custom_fields: dict[str, str] | None = None,
    user_key: bytes,
) -> dict[str, object]:
    """Build the JSON payload for POST /api/ciphers.

    Encrypts ``note_name``, ``body_text``, and every custom
    field's name+value with the user key. Vaultwarden
    rejects plaintext cipher fields — the API expects every
    string field to be a Type-2 envelope.

    Args:
      note_name:    Display name shown in the Vaultwarden UI
                    (e.g. "cloudflared k8s secret value").
      body_text:    Raw value VKS will write into the
                    Secret's data[key]. Bytes are
                    UTF-8 encoded before encryption.
      custom_fields: {field_name: field_value, ...}. For
                    VKS, this is the triple namespaces /
                    secret-name / secret-key. Optional —
                    omit for a vanilla note.
      user_key:     The 64-byte unwrapped user key from
                    ``VaultwardenClient.user_key``.

    Returns:
      The JSON-shaped dict ready to POST to /api/ciphers.
    """
    fields = []
    for fname, fvalue in (custom_fields or {}).items():
        fields.append(
            {
                "type": FIELD_TYPE_TEXT,
                "name": encrypt_str_for_vault(fname, user_key),
                "value": encrypt_str_for_vault(fvalue, user_key),
            }
        )
    return {
        "type": TYPE_SECURE_NOTE,
        "name": encrypt_str_for_vault(note_name, user_key),
        "notes": encrypt_str_for_vault(body_text, user_key),
        "secureNote": {"type": SECURE_NOTE_GENERIC},
        "fields": fields,
        "favorite": False,
    }


def vks_triple(
    *,
    namespace: str,
    secret_name: str,
    secret_key: str,
) -> dict[str, str]:
    """Build the VKS custom-fields dict from the orchestrator's
    concrete Kubernetes coordinates.

    Convenience helper: this is the only shape of
    ``custom_fields`` the orchestrator ever needs.
    """
    return {
        VKS_FIELD_MAP["namespaces"]: namespace,
        VKS_FIELD_MAP["secret-name"]: secret_name,
        VKS_FIELD_MAP["secret-key"]: secret_key,
    }
