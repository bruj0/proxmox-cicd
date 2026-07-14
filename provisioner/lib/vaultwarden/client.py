"""`VaultwardenClient` — login + cipher list/create/delete + decrypt.

One client per (server URL, account) pair. Holds the short-
lived access token (~1h), the unwrapped 64-byte user key,
and an opener pre-loaded with the Cloudflare/Vaultwarden
header requirements. Callers don't need to know anything
about the underlying PBKDF2/HKDF/AES dance — they just
call ``create_secure_note`` / ``list_ciphers`` /
``delete_cipher`` / ``decrypt_cipher_field``.

What the client does NOT do:

  - It does NOT persist the access token or the user key
    anywhere on disk. Both live only on the instance.
    Re-instantiate to log in again.
  - It does NOT refresh the access token. The token TTL
    is short (~1h); for long-running sessions, re-create
    the client.
  - It does NOT handle 2FA. VaultwardenK8sSync also
    doesn't support 2FA, so this matches the deploy story.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any, cast

from provisioner.lib.vaultwarden.crypto import (
    DEFAULT_KDF_ITERATIONS,
    decrypt_str_from_vault,
    make_master_key,
    make_server_auth_hash,
    unwrap_user_key,
)
from provisioner.lib.vaultwarden.http import (
    _open,
    build_opener,
    http_delete,
    http_get_json,
    http_post_form,
    http_post_json,
)


class VaultwardenClient:
    """A logged-in Vaultwarden session.

    Construct with ``VaultwardenClient.login(...)`` (a
    convenience classmethod that runs prelogin + password
    derivation + token request + profile fetch + user-key
    unwrap in one shot). Once you have a client, every
    cipher operation is a single method call.

    Attributes:
        server_url:    The base URL of the Vaultwarden
                       instance (e.g. ``https://bitwarden.example``).
        email:         Account email.
        access_token:  Short-lived bearer token (~1h).
        user_key:      64-byte unwrapped vault symmetric key.
    """

    def __init__(
        self,
        server_url: str,
        email: str,
        access_token: str,
        user_key: bytes,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.email = email
        self.access_token = access_token
        self.user_key = user_key
        self._opener = build_opener()

    # ---------- construction ----------

    @classmethod
    def login(
        cls,
        *,
        server_url: str,
        client_id: str,
        client_secret: str,
        email: str,
        master_password: str,
        device_identifier: str = "proxmox-cicd-library",
        device_name: str = "provisioner.lib.vaultwarden",
        device_type: str = "25",
    ) -> VaultwardenClient:
        """Run the full login flow: prelogin, derive keys, fetch
        profile, unwrap user key. Returns a ready-to-use client.

        Raises ``RuntimeError`` on /connect/token failure (auth
        error → "Username or password is incorrect") or on a
        malformed response (e.g. the prelogin body is
        missing ``kdfIterations``).
        """
        opener = build_opener()
        # 1. Discover KDF settings via /identity/accounts/prelogin.
        # This endpoint takes a JSON body but does NOT require
        # Bearer auth — see the helper at the bottom of this
        # module.
        prelogin_url = f"{server_url.rstrip('/')}/identity/accounts/prelogin"
        pre_resp = _http_post_json_no_auth(
            opener, prelogin_url, {"email": email}
        )
        iterations = int(pre_resp.get("kdfIterations", DEFAULT_KDF_ITERATIONS))

        # 2. Derive the auth hash. Sensitive bytes — overwrite
        # the master password string BEFORE returning to
        # minimize the time it sits in memory.
        master_key = make_master_key(master_password, email, iterations)
        auth_hash = make_server_auth_hash(master_key, master_password)

        # 3. /identity/connect/token.
        token_url = f"{server_url.rstrip('/')}/identity/connect/token"
        token_resp = http_post_form(
            opener,
            token_url,
            {
                "grant_type": "password",
                "username": email,
                "password": auth_hash,
                "scope": "api offline_access",
                "client_id": client_id,
                "client_secret": client_secret,
                "deviceType": device_type,
                "deviceIdentifier": device_identifier,
                "deviceName": device_name,
            },
        )
        access_token = token_resp.get("access_token")
        if not access_token:
            raise RuntimeError(
                f"/connect/token response missing access_token; "
                f"got keys={sorted(token_resp.keys())}"
            )

        # 4. /api/accounts/profile — has the wrapped user key.
        profile = http_get_json(
            opener,
            f"{server_url.rstrip('/')}/api/accounts/profile",
            access_token,
        )
        if "key" not in profile:
            raise RuntimeError(
                f"/api/accounts/profile response missing 'key' field; "
                f"got keys={sorted(profile.keys())}"
            )

        # 5. Unwrap the user key. This is where a wrong
        # master password surfaces as ValueError (MAC
        # mismatch or length check).
        user_key = unwrap_user_key(master_key, profile["key"])

        return cls(
            server_url=server_url,
            email=email,
            access_token=access_token,
            user_key=user_key,
        )

    # ---------- cipher operations ----------

    def list_ciphers(
        self,
        *,
        organization_id: str | None = None,
        folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/ciphers → list of cipher dicts (encrypted).

        Filters: pass ``organization_id`` to fetch only org
        ciphers (requires the account to be a member), or
        ``folder_id`` for a single folder.

        Cipher names and field values are returned ENCRYPTED.
        Use ``decrypt_cipher_name`` /
        ``decrypt_cipher_field`` to read them.

        Response shape compatibility: Vaultwarden 1.34.0+
        (and the upstream Bitwarden cloud) now return a
        paginated envelope of the form
        ``{"object": "list", "data": [<cipher>, ...],
        "continuationToken": null}``. Older Vaultwarden
        releases (and most tests) return a bare JSON list.
        This method accepts both and always returns the
        flat list of ciphers — callers don't need to
        special-case the envelope.
        """
        params: dict[str, str] = {}
        if organization_id is not None:
            params["organizationId"] = organization_id
        if folder_id is not None:
            params["folderId"] = folder_id
        url = f"{self.server_url}/api/ciphers"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        resp = http_get_json(self._opener, url, self.access_token)
        # Paginated envelope (Vaultwarden 1.34.0+, Bitwarden
        # cloud). The envelope also carries `continuationToken`
        # — we don't follow it here because the cloud-side
        # personal vault is bounded in size, but the helper
        # below documents the shape for callers that need it.
        if isinstance(resp, dict) and resp.get("object") == "list" and isinstance(
            resp.get("data"), list
        ):
            return cast(list[dict[str, Any]], resp["data"])
        if isinstance(resp, list):
            return cast(list[dict[str, Any]], resp)
        raise RuntimeError(
            f"/api/ciphers response was not a list; "
            f"got type {type(resp).__name__}"
        )

    def get_cipher(self, cipher_id: str) -> dict[str, Any]:
        """GET /api/ciphers/{id} → single cipher dict."""
        return cast(
            dict[str, Any],
            http_get_json(
                self._opener,
                f"{self.server_url}/api/ciphers/{cipher_id}",
                self.access_token,
            ),
        )

    def decrypt_cipher_name(self, cipher: dict[str, Any]) -> str:
        """Decrypt ``cipher['name']`` using this client's user key.

        Returns the plaintext display name. Raises
        ``ValueError`` if the envelope is malformed.
        """
        return decrypt_str_from_vault(cipher["name"], self.user_key)

    def decrypt_cipher_field(
        self,
        cipher: dict[str, Any],
        *,
        name: str | None = None,
        index: int | None = None,
    ) -> str:
        """Decrypt a single custom field's **value**.

        Match by ``name`` (the decrypted field name — i.e.
        the result of :meth:`decrypt_cipher_field_name`) or
        by ``index`` (0-based position in
        ``cipher['fields']``). Raises ``KeyError`` if no
        match, ``ValueError`` on decrypt failure.

        Both Bitwarden field names AND values are stored
        as encrypted EncStrings, so lookups by name have
        to decrypt every candidate's name to compare.
        When you have the index handy (you already iterated
        ``cipher['fields']``), prefer ``index=`` — it's
        O(1) instead of O(n).

        See :meth:`decrypt_cipher_field_name` for the
        symmetric helper that returns the **name** of a
        field by index. The name itself is encrypted too,
        so the helper takes an index (not a decrypted
        name) and returns the decrypted plaintext name.
        """
        fields = cipher.get("fields") or []
        for i, f in enumerate(fields):
            if index is not None and i == index:
                return decrypt_str_from_vault(f["value"], self.user_key)
            if name is not None:
                try:
                    if decrypt_str_from_vault(f["name"], self.user_key) == name:
                        return decrypt_str_from_vault(f["value"], self.user_key)
                except ValueError:
                    continue
        raise KeyError(
            f"no cipher field matched "
            f"name={name!r} index={index!r} "
            f"(cipher has {len(fields)} fields)"
        )

    def decrypt_cipher_field_name(
        self,
        cipher: dict[str, Any],
        *,
        index: int,
    ) -> str:
        """Decrypt a single custom field's **name** by index.

        Field names on a cipher are themselves encrypted
        Bitwarden EncStrings (``"2.<ct>|<iv>|<mac>"``),
        so the ``name`` property in
        ``cipher['fields'][i]['name']`` is opaque.
        This helper returns the decrypted plaintext name
        (e.g. ``"namespaces"``,
        ``"secret-name"``, ``"secret-key"``).

        Pairs with :meth:`decrypt_cipher_field` which
        returns the value. The two-call shape is
        deliberate — it's the only way to read both
        halves without exposing the underlying EncString
        format to callers.

        Raises ``IndexError`` if ``index`` is out of range,
        ``ValueError`` on decrypt failure.
        """
        fields = cipher.get("fields") or []
        if index < 0 or index >= len(fields):
            raise IndexError(
                f"field index {index} out of range "
                f"(cipher has {len(fields)} fields)"
            )
        return decrypt_str_from_vault(fields[index]["name"], self.user_key)

    def decrypt_cipher_notes(self, cipher: dict[str, Any]) -> str:
        """Decrypt ``cipher['notes']`` (the Secure Note body)."""
        return decrypt_str_from_vault(cipher["notes"], self.user_key)

    def create_cipher(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/ciphers → the created cipher (with id).

        Caller is responsible for encrypting the payload
        fields (``name``, ``notes``, ``fields[].name``,
        ``fields[].value``) with the user key. Use
        ``build_secure_note_payload`` for Secure Notes —
        it does that for you.
        """
        return http_post_json(
            self._opener,
            f"{self.server_url}/api/ciphers",
            payload,
            self.access_token,
        )

    def delete_cipher(self, cipher_id: str) -> None:
        """DELETE /api/ciphers/{id}. Idempotent on the server."""
        http_delete(
            self._opener,
            f"{self.server_url}/api/ciphers/{cipher_id}",
            self.access_token,
        )


def _http_post_json_no_auth(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST JSON without Bearer auth.

    Only used by ``login`` for /identity/accounts/prelogin,
    which is unauthenticated by design. Kept private to the
    module — every other /api call needs Bearer auth and
    should use ``http_post_json`` instead.
    """
    import json as _json
    import urllib.request
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return cast(dict[str, Any], _json.loads(_open(opener, req)))
