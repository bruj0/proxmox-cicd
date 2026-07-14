"""HTTP helpers for talking to Vaultwarden + Bitwarden-cloud.

The Bitwarden / Vaultwarden REST surface has a few
non-obvious header requirements that this module pins down
in one place:

1. **User-Agent** — Cloudflare's edge (in front of most
   public Bitwarden instances) and a few self-hosted
   Vaultwarden deployments WAF-block the default
   ``Python-urllib/X.Y`` UA. A generic curl-shaped UA
   passes through.

2. **Bitwarden-Client-Version** — REQUIRED by
   ``/identity/connect/token``. Vaultwarden's ``auth.rs``
   ``FromRequest`` impl rejects requests with "No
   Bitwarden-Client-Version header provided" BEFORE the
   password check even runs. Value must parse as semver
   (``YYYY.MM.PATCH``).

3. **device-type** — read by Vaultwarden's
   ``ClientHeaders`` ``FromRequest`` impl to populate the
   device row. Optional but recommended. Use ``25``
   (LinuxCLI) so the Devices page shows this client as a
   CLI tool — matches what the official ``bw`` CLI sends.

``build_opener()`` returns an ``urllib.request.OpenerDirector``
with all three headers baked in, so every call site picks
them up automatically.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final, cast


# Curl-shaped UA: passes the Cloudflare edge WAF that blocks
# the default `Python-urllib/X.Y` UA.
DEFAULT_USER_AGENT: Final = "curl/8.5.0"

# Semver `YYYY.MM.PATCH`. Required by Vaultwarden's
# /identity/connect/token endpoint. We pin to the current
# web-vault version so the server can't reasonably reject
# us as too-old.
DEFAULT_CLIENT_VERSION: Final = "2025.12.0"

# Bitwarden's DeviceType enum. We use 25 (LinuxCLI) to
# match the official `bw` CLI's device-type header.
DEFAULT_DEVICE_TYPE: Final = "25"


class VaultwardenHTTPError(RuntimeError):
    """Non-2xx response from Vaultwarden.

    Promotes ``urllib.error.HTTPError`` to a typed exception
    that carries the URL + status + first 300 chars of the
    response body. Callers can ``except VaultwardenHTTPError``
    instead of having to catch ``HTTPError`` and unpack it
    themselves.
    """

    def __init__(self, url: str, code: int, body: str) -> None:
        snippet = body.strip()[:300]
        super().__init__(f"Vaultwarden HTTP {code} for {url}: {snippet}")
        self.url = url
        self.code = code
        self.body = body


def build_opener() -> urllib.request.OpenerDirector:
    """Build an opener pre-loaded with the three required headers.

    Returns a fresh opener each call (Python's urllib openers
    carry request-state through ``.open``; building a new
    one is the canonical way to reset between clients).
    """
    opener = urllib.request.build_opener()
    opener.addheaders = [
        ("User-Agent", DEFAULT_USER_AGENT),
        ("Bitwarden-Client-Version", DEFAULT_CLIENT_VERSION),
        ("device-type", DEFAULT_DEVICE_TYPE),
    ]
    return opener


def _open(opener: urllib.request.OpenerDirector, req: urllib.request.Request) -> str:
    """Single chokepoint for ``opener.open``.

    Translates non-2xx responses into ``VaultwardenHTTPError``
    so callers don't have to wrap every call in try/except.
    Honors HTTP method on the Request (``req.get_method()``);
    treats 2xx and 3xx as success (urllib follows redirects
    by default).
    """
    url = req.get_full_url() if hasattr(req, "get_full_url") else req.full_url
    try:
        with opener.open(req, timeout=30) as resp:
            return cast(str, resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise VaultwardenHTTPError(url, exc.code, body) from None


def http_post_form(
    opener: urllib.request.OpenerDirector,
    url: str,
    form: dict[str, str],
) -> dict[str, Any]:
    """POST application/x-www-form-urlencoded → JSON dict.

    Raises ``VaultwardenHTTPError`` on non-2xx and
    ``json.JSONDecodeError`` (wrapped in ``RuntimeError``)
    on non-JSON success bodies.
    """
    data = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    body = _open(opener, req)
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"non-JSON response from {url}: {body[:500]}"
        ) from exc


def http_post_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, Any],
    access_token: str,
) -> dict[str, Any]:
    """POST application/json with Bearer auth → JSON dict.

    The access token is sent in the ``Authorization`` header,
    not as a form field — Bitwarden's ``/api/ciphers`` and
    similar endpoints expect this form.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    body = _open(opener, req)
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"non-JSON response from {url}: {body[:500]}"
        ) from exc


def http_get_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    access_token: str,
) -> dict[str, Any]:
    """GET with Bearer auth → JSON dict."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    body = _open(opener, req)
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"non-JSON response from {url}: {body[:500]}"
        ) from exc


def http_delete(
    opener: urllib.request.OpenerDirector,
    url: str,
    access_token: str,
) -> None:
    """DELETE with Bearer auth. No content returned; raises on non-2xx."""
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _open(opener, req)
