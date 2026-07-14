"""cloudflared_tunnel — orchestrate a remotely-managed
Cloudflare Tunnel using only its API token.

Background — what Cloudflare's API actually returns
for a remotely-managed tunnel (`config_src=cloudflare`,
the only kind the upstream chart supports):

  POST /accounts/:id/cfd_tunnel  with body
    `{"name": "...", "config_src": "cloudflare"}`
  returns (excerpt):
    {
      "id":    "<UUID>",
      "name":  "...",
      "config_src": "cloudflare",
      "credentials_file": {
        "AccountTag":   "<account>",
        "TunnelID":     "<UUID without dashes>",
        "TunnelName":   "...",
        "TunnelSecret": "<base64 blob>"
      },
      "token":  "<base64-string>"
    }

The single auth artifact cloudflared needs is the
`result.token` field — a base64 string that decodes
to compact JSON `{"a":..., "t":..., "s":...}`. It is
NOT a JWT triple, NOT cert.pem, NOT a per-host key
file. For remotely-managed tunnels, this single
opaque string is the **only** authentication
material; cloudflared 2024.8.3 accepts it via the
`TUNNEL_TOKEN` env var / `--token` flag and uses it
verbatim (no decoding on the client side).

The same value can be re-fetched at any time via
GET /accounts/:id/cfd_tunnel/:tun/token, which
returns the same base64 string as `result`. Both
endpoints are accepted as the source of truth;
this module prefers the GET path on rotation
because it works on existing tunnels without
needing to mint a new one.

Why no JWT path: Cloudflare's docs and source code
treat "tunnel token" as a colloquial name for the
base64 string above. There is no API endpoint that
returns a true `a.b.c` JWT, and there is no JWT
validation on cloudflared's side for the remotely-
managed path. cert.pem is only used for
**locally-managed** tunnels (the legacy `cloudflared
tunnel login` flow), which this orchestrator does
not exercise.

This module is the three-lifecycle helper:

  - mint     -> POST /cfd_tunnel, return a TunnelRecord
                carrying the canonical credentials dict
                plus the bearer token.
  - list_by_name -> GET .../cfd_tunnel?name=...  to
                detect an existing tunnel under the
                same name without forging one.
  - delete   -> DELETE /cfd_tunnel/{id}
  - rotate   -> delete + mint (idempotent on name).

  Plus a `persist(record)` helper that writes the
  canonical record to disk in mode 0600.

References:
  - https://developers.cloudflare.com/api/resources/\
      zero_trust/subresources/tunnels/subresources/\
      cloudflared/subresources/token/methods/get/
      (GET /cfd_tunnel/:id/token)
  - https://developers.cloudflare.com/cloudflare-one/\
      networks/connectors/cloudflare-tunnel/\
      get-started/create-remote-tunnel-api/
      (POST /cfd_tunnel)
  - https://developers.cloudflare.com/cloudflare-one/\
      networks/connectors/cloudflare-tunnel/\
      configure-tunnels/run-parameters/
      (`TUNNEL_TOKEN` is for remotely-managed
      tunnels only; `--origincert` is for locally-
      managed tunnels only)
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CF_API_BASE = "https://api.cloudflare.com/client/v4"
_CF_API_TIMEOUT_S = 30.0


class _CfError(RuntimeError):
    """Raised on a Cloudflare API 4xx/5xx with a sanitised
    error payload. The runtime trace is preserved for
    debugging.
    """


def _cf_request(
    method: str,
    path: str,
    *,
    token_value: str | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Same semantics as the orchestrator's CF wrapper.
    Kept inline so this module has zero cross-deps and is
    unit-testable in isolation.
    """
    url = CF_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token_value is not None:
        headers["Authorization"] = f"Bearer {token_value}"
    req = urllib.request.Request(
        url=url, data=data, method=method, headers=headers
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(
            req, timeout=_CF_API_TIMEOUT_S, context=ctx
        ) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise _CfError(
            f"Cloudflare API {method} {path} failed: "
            f"HTTP {e.code} {e.reason}: {err_body}"
        ) from e
    except urllib.error.URLError as e:
        raise _CfError(
            f"Cloudflare API {method} {path} "
            f"connection error: {e.reason}"
        ) from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _CfError(
            f"Cloudflare API {method} {path} returned "
            f"non-JSON body: {raw[:200]!r}"
        ) from e
    if not payload.get("success", True):
        raise _CfError(
            f"Cloudflare API {method} {path} returned "
            f"success=false: {payload.get('errors')}"
        )
    return payload.get("result", payload)


def looks_like_tunnel_token(value: str) -> bool:
    """Cheap, allocation-light check for "this string looks
    like the base64-encoded compact-JSON tunnel token
    cloudflared accepts via `$TUNNEL_TOKEN`."

    A valid token:
      - starts with `eyJ` (base64 of `{`)
      - is decodable as base64 → UTF-8 → JSON object
      - has the three canonical keys `a` (AccountTag),
        `t` (TunnelID), `s` (TunnelSecret)

    Returns True iff all three hold. False on any parse
    failure or shape mismatch. Use this when reading a
    cached record from disk — if it returns False the
    cached value is corrupted (e.g. it's the *decoded*
    compact JSON instead of the base64) and the caller
    should rotate the tunnel.
    """
    if not isinstance(value, str) or not value.startswith("eyJ"):
        return False
    try:
        raw = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    try:
        compact = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(compact, dict):
        return False
    return all(k in compact and compact[k] for k in ("a", "t", "s"))


def decode_credentials_blob(
    blob: str | dict[str, Any],
) -> dict[str, str]:
    """Normalize the credentials_file content into a dict
    with `AccountTag`, `TunnelID`, `TunnelSecret`,
    `TunnelName` keys.

    Inputs supported:
      - `{"AccountTag": ..., "TunnelID": ..., ...}` — when
        Cloudflare returns it directly in
        `result.credentials_file`.
      - `eyJh...dW5uZWwifQ==` (base64 of
        `{"a":..., "t":..., "s":...}`) — when Cloudflare
        returns the field in `result.token`.

    Both forms encode the same shape and both are accepted
    by `cloudflared --credentials-file <path>`. We
    normalize the keys so downstream readers see a
    consistent dict.
    """
    if isinstance(blob, dict):
        compact = {
            "a": blob.get("AccountTag", ""),
            "t": blob.get("TunnelID", "").replace("-", ""),
            "s": blob.get("TunnelSecret", ""),
            # Carry `name` through so the dict-input path
            # produces the same `TunnelName` as the
            # base64-string path would.
            "name": blob.get("TunnelName", ""),
        }
    elif isinstance(blob, str):
        decoded = base64.b64decode(blob).decode("utf-8")
        compact = json.loads(decoded)
    else:
        raise _CfError(
            f"credentials blob has unsupported type: "
            f"{type(blob).__name__}"
        )
    a = str(compact.get("a", ""))
    t = str(compact.get("t", ""))
    s = str(compact.get("s", ""))
    if not (a and t and s):
        raise _CfError(
            f"credentials blob missing required keys: "
            f"a={a[:8]!r}, t={t[:8]!r}, "
            f"s={'<set>' if s else '<empty>'}"
        )
    return {
        "AccountTag": a,
        "TunnelID": t,
        "TunnelName": str(compact.get("name", "")),
        "TunnelSecret": s,
    }


@dataclass
class TunnelRecord:
    """Canonical tunnel record consumed by the orchestrator.

    `id` is the UUID returned by Cloudflare. `name` is the
    registered tunnel name (e.g. `cicd-tunnel`). `token`
    is the **base64-encoded bearer string** cloudflared
    reads from `$TUNNEL_TOKEN` (the chart wires this env
    var from the Secret's `tunnelToken` key). `credentials_file`
    is the decoded dict shape `{"AccountTag", "TunnelID",
    "TunnelSecret", "TunnelName"}` — kept for reference /
    rotation flows; the chart only needs `token`.
    """

    id: str
    name: str
    token: str
    credentials_file: dict[str, str] = field(default_factory=dict)

    def credentials_file_path(self) -> str:
        """Path the chart would mount into the cloudflared
        pod under the `--credentials-file` flag. Not used
        by the current chart (which uses `$TUNNEL_TOKEN`
        env), but documented for future migration.
        """
        return f"/etc/cloudflared/{self.id}.json"


@dataclass
class CloudflaredTunnelClient:
    """HTTP client for creating / listing / deleting
    remotely-managed Cloudflare Tunnels. Returned
    TunnelRecord objects are ready to feed to the chart.
    """

    token_value: str

    def mint(
        self,
        account_id: str,
        tunnel_name: str,
        config_src: str = "cloudflare",
    ) -> TunnelRecord:
        """POST /accounts/:id/cfd_tunnel with body
        `{name, config_src: cloudflare}`. Accepts the
        response in either of the two shapes Cloudflare
        has shipped in 2024–2026 (decoded `credentials_file`
        dict, or `token` base64 of the same content).
        Returns a TunnelRecord with the canonical `token`
        (base64 string cloudflared consumes verbatim via
        `$TUNNEL_TOKEN`) plus the decoded `credentials_file`.
        """
        body = {"name": tunnel_name, "config_src": config_src}
        result = _cf_request(
            "POST",
            f"/accounts/{account_id}/cfd_tunnel",
            token_value=self.token_value,
            body=body,
        )
        if not isinstance(result, dict):
            raise _CfError(
                f"POST /cfd_tunnel returned non-dict: {result!r}"
            )
        # Prefer the documented `token` field (base64
        # string). Fall back to `credentials_file` (decoded
        # dict) by re-encoding it to the canonical base64
        # form so cloudflared gets the same input either way.
        token_raw = result.get("token")
        if token_raw:
            credentials_file = decode_credentials_blob(token_raw)
            token = token_raw
        else:
            creds_dict = result.get("credentials_file")
            if creds_dict is None:
                raise _CfError(
                    "POST /cfd_tunnel returned neither "
                    "credentials_file nor token: "
                    f"{sorted(result.keys())}"
                )
            credentials_file = decode_credentials_blob(creds_dict)
            # Re-encode to the canonical base64 form
            compact = {
                "a": credentials_file["AccountTag"],
                "t": credentials_file["TunnelID"],
                "s": credentials_file["TunnelSecret"],
            }
            token = base64.b64encode(
                json.dumps(compact, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
        tunnel_id = str(result["id"])
        # `decode_credentials_blob` may have stripped
        # dashes (when fed the base64 form). Always end
        # up with the dashed form in credentials_file.
        credentials_file["TunnelID"] = tunnel_id
        return TunnelRecord(
            id=tunnel_id,
            name=str(result["name"]),
            token=token,
            credentials_file=credentials_file,
        )

    def delete(self, account_id: str, tunnel_id: str) -> None:
        """DELETE /accounts/:id/cfd_tunnel/:id. Soft-deletes
        the tunnel so it can be re-created under the same
        name with fresh credentials. Idempotent.
        """
        result = _cf_request(
            "DELETE",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            token_value=self.token_value,
        )
        if not isinstance(result, dict) or not result.get(
            "success", False
        ):
            raise _CfError(
                f"DELETE /cfd_tunnel/{tunnel_id} returned "
                f"non-success: {result!r}"
            )

    def list_by_name(
        self, account_id: str, tunnel_name: str
    ) -> list[dict[str, Any]]:
        """GET /accounts/:id/cfd_tunnel?name=...&is_deleted=false.
        Returns the raw tunnel dicts (without credentials).
        Used by the orchestrator to detect existing tunnels
        without forging one.
        """
        result = _cf_request(
            "GET",
            f"/accounts/{account_id}/cfd_tunnel",
            token_value=self.token_value,
            query={"name": tunnel_name, "is_deleted": "false"},
        )
        if isinstance(result, list):
            return list(result)
        tunnels = (result or {}).get("tunnels", [])
        return list(tunnels) if isinstance(tunnels, list) else []

    def rotate(
        self,
        account_id: str,
        tunnel_id: str,
        tunnel_name: str,
        config_src: str = "cloudflare",
    ) -> TunnelRecord:
        """Refresh the credentials by re-creating the tunnel
        under a fresh UUID. There is no in-place refresh
        endpoint for tunnel credentials.
        """
        self.delete(account_id, tunnel_id)
        return self.mint(account_id, tunnel_name, config_src)


def persist(record: TunnelRecord, path: Path) -> Path:
    """Persist a TunnelRecord as JSON at `path` (mode 0600).

    Schema:
      {
        "id":    "<UUID>",
        "name":  "...",
        "tunnel_token": "<base64-string>",  # what cloudflared reads
        "credentials_file": {                # decoded, for reference
          "AccountTag":   "...",
          "TunnelID":     "<UUID>",
          "TunnelName":   "...",
          "TunnelSecret": "..."
        }
      }
    """
    payload = {
        "id": record.id,
        "name": record.name,
        "tunnel_token": record.token,
        "credentials_file": record.credentials_file,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path
