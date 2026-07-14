"""cloudflared app — exposes Gateway-fronted services on a
public Cloudflare URL via a **remotely-managed** Cloudflare
Tunnel.

Source: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
Chart:  https://github.com/cloudflare/helm-charts/releases/tag/cloudflare-tunnel-remote-0.1.2
        (vendored at infra/helm-charts/cloudflare-tunnel-remote-0.1.2.tgz)

What this app does on apply (idempotent, replay-safe):

  1. Reads Cloudflare account + zone identifiers from
     `.env` (CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID,
     CLOUDFLARE_DOMAIN, CLOUDFLARE_GLOBAL_API_KEY,
     CLOUDFLARE_GLOBAL_API_EMAIL).
  2. If `infra/secrets/cloudflared-api-token.json` exists,
     reads the cached scoped API token (id + value) and
     uses it for every subsequent Cloudflare API call.
     Otherwise it mints a fresh scoped token via
     `POST /user/tokens` (the `CLOUDFLARE_GLOBAL_API_KEY`
     global-API-key + email authenticate this one-shot
     call only; the global key is never persisted, and
     is never used again after the token is minted). The
     mint grants the minimum scope:
       - Account:Cloudflare Tunnel:Edit (for the
         cfd_tunnel API)
       - Zone:DNS:Edit (for the CNAME record)
  3. Ensures a **remotely-managed** Cloudflare Tunnel
     named `cicd-tunnel` exists on the account
     (`config_src=cloudflare`, POST
     /accounts/:id/cfd_tunnel if absent). Captures the
     tunnel UUID and the JSON `TunnelSecret` blob.
  4. Fetches the **tunnel credentials blob** via
     `GET /accounts/:acc/cfd_tunnel/:tun/token`. The
     body is a base64-encoded compact-JSON document
     `{"a": "<accountTag>", "t": "<tunnelUUID>",
     "s": "<tunnelSecret>"}` — this is the upstream
     "credentials file" content, not a JWT triple.
     Persists it to
     `infra/secrets/cloudflared-tunnel.json["tunnel_token"]`
     (mode 0600, gitignored). See **Known issue**
     below re: how cloudflared 2024.8.3 consumes this.
  5. Pushes a **remote ingress rule** to Cloudflare:
     `PUT /accounts/:acc/cfd_tunnel/:tun/configurations`
     with a single rule that fans `<hostname> -> http://
     <envoy-svc>:80` and a catch-all `http_status:404`.
     The remotely-managed chart pulls this config down
     on each connection (no local `config.yaml` mount,
     no `cert.pem` needed).
  6. Seeds the JWT into Vaultwarden as a Secure Note
     `(app=cloudflared, namespace=cloudflared,
     secret-name=cloudflare-tunnel-remote,
     secret-key=tunnelToken)` so VaultwardenK8sSync
     recreates the chart-managed Secret if helm ever
     deletes it.
  7. Installs the upstream
     `cloudflare-tunnel-remote-0.1.2` chart into the
     cluster (`replicaCount=1`, `image.tag=2024.8.3`,
     `cloudflare.tunnel_token=<base64-credentials-json>`).
     The chart creates
     Secret/cloudflare-tunnel-remote with key
     `tunnelToken`, the Deployment mounts it as
     `$TUNNEL_TOKEN`. See **Known issue** below.
  8. Ensures a proxied CNAME record on the zone:
     `<hostname> -> <tunnel-uuid>.cfargotunnel.com`
     (orange-clouded). The hostname defaults to
     `gitea.<base_domain>` from `catalog.ingress`.

Idempotency: every step is no-op if state already
matches the desired config. Re-running `cicdctl apply
cicd` after a successful first install is a no-op (the
scoped token is cached, the tunnel exists, the
credentials blob is cached, the ingress rule is at
version=N, the DNS record exists, the helm release is
at the pinned chart version).

Security model:
  - The global API key is read once per machine and
    used only to mint the scoped token. After the mint,
    it is never written to disk and never used again
    by this app.
  - The scoped token + the tunnel secret + the
    credentials blob are the only credentials persisted
    on disk; all three live under `infra/secrets/`
    (mode 0600, gitignored).
  - The scoped token can be revoked at any time via
    `DELETE /user/tokens/<id>`. The next apply would
    re-mint a new one automatically.

Known issue (open as of 2026-07-14):
  The chart's Secret/cloudflare-tunnel-remote/
  `tunnelToken` key holds the **base64-encoded
  credentials-JSON document** fetched in step 4, NOT a
  true JWT triple. Today the chart Deployment surfaces
  it as `$TUNNEL_TOKEN`, and cloudflared 2024.8.3
  rejects it with:
      Provided Tunnel token is not valid.
      See 'cloudflared tunnel run --help'.
  Verified by spinning up a one-shot pod with the
  exact same image and env var; same error, no chart
  involvement. So this is **not** a chart problem
  — it's a runtime fact about how 2024.8.3 parses
  $TUNNEL_TOKEN.
  Workarounds to try (not implemented in this
  revision):
    a. Replace the chart's `tunnel_token`-as-env
       with `--credentials-file
       /etc/cloudflared/<UUID>.json` mounted from a
       Secret holding the JSON. cloudflared then
       reads the JSON natively.
    b. Bump APP_VERSION to a newer image (e.g.
       2026.7.1) which may accept the credentials
       blob in $TUNNEL_TOKEN.
    c. Mint a true JWT via `cloudflared tunnel
       token <name>` from a host that has the
       account cert.pem — the only API path that
       issues the a.b.c triple.
  Until one of the above lands, step 5–7 succeed
  (chart installs, Secret populated, ingress rule
  pushed, DNS CNAME live, VWS note seeded) but
  gitea.bruj0.net returns HTTP 530 — the *pod*
  cannot dial Cloudflare. Step 4 (the API fetch) is
  itself correct and idempotent.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..container import Container
from . import AppApplyResult, AppPlanResult, AppStatus, register

# Chart constants. Pinned to chart 0.1.2 + appVersion 2024.8.3.
CHART_TGZ = Path("infra/helm-charts/cloudflare-tunnel-remote-0.1.2.tgz")
CHART_VERSION = "0.1.2"
APP_VERSION = "2024.8.3"
NAMESPACE = "cloudflared"
HELM_RELEASE_NAME = "cloudflare-tunnel-remote"

# Tunnel name on the Cloudflare account. Stable; if you
# change this, the apply will create a new tunnel (the
# old one must be cleaned up separately — `cloudflared
# tunnel delete` is a future-work item).
TUNNEL_NAME = "cicd-tunnel"

# Where the scoped API token, tunnel credentials, and
# JWT tunnel token live on the operator's host. Mode
# 0600; gitignored.
HOST_TOKEN_FILE = Path("infra/secrets/cloudflared-api-token.json")
HOST_TUNNEL_FILE = Path("infra/secrets/cloudflared-tunnel.json")

# Cloudflare API base. v4 is current; tunnels live under
# /accounts/:id/cfd_tunnel (not /zones/:id/...).
CF_API_BASE = "https://api.cloudflare.com/client/v4"

# Cloudflare permission-group UUIDs (stable, not names).
_PERM_GROUP_TUNNEL_EDIT = (
    "c07321b023e944ff818fec44d8203567",
    "Cloudflare Tunnel Write",
)
_PERM_GROUP_DNS_EDIT = ("4755a26eedb94da69e1066d98aa820be", "DNS Write")

# Default upstream: the Envoy Gateway Service that fronts
# the gitea Gateway. Resolved at apply time via kubectl;
# see `_envoy_service_for_gateway`. The chart-bundled
# envoy-gateway controller emits two labels per proxy
# Service: `owning-gateway-name` +
# `owning-gateway-namespace`. We match on both so the
# lookup is unambiguous.
_ENVOY_GW_NAMESPACE = "envoy-gateway-system"
_ENVOY_GW_LABEL_NAME = "gateway.envoyproxy.io/owning-gateway-name"
_ENVOY_GW_LABEL_NAMESPACE = "gateway.envoyproxy.io/owning-gateway-namespace"

# Vaultwarden-side note coordinates. The note pushed by
# the orchestrator matches what VaultwardenK8sSync
# expects: app/namespace/secret-name/secret-key. We
# deliberately target the chart-managed Secret name
# (`cloudflare-tunnel-remote`) so VKS recreates the
# chart's Secret on delete/recreate, not a custom one.
VWS_NOTE_APP = "cloudflared"
VWS_NOTE_NAMESPACE = "cloudflared"
VWS_NOTE_SECRET_NAME = "cloudflare-tunnel-remote"
VWS_NOTE_SECRET_KEY = "tunnelToken"

# Default Gateway we forward to. The cluster already
# has `gitea/gitea` provisioned at apply time.
DEFAULT_GATEWAY_NAMESPACE = "gitea"
DEFAULT_GATEWAY_NAME = "gitea"

# Sensible defaults; overridden by the env file.
_CF_API_TIMEOUT_S = 30.0

# Operator-scoped values (mode 0600). Used in `apply` to
# mint the scoped API token on first run only.
_MIN_TOKEN_LIFETIME_S = 24 * 3600


def _looks_like_jwt(value: str) -> bool:
    """True if `value` has the JWT shape (3 base64 chunks
    joined by dots). Used to deduplicate the
    credentials-vs-JWT call to Cloudflare's /token endpoint.
    """
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(p for p in parts)


@dataclass
class CloudflaredApp:
    """AppSpec for the cloudflared tunnel (remotely-managed)."""

    name: str = "cloudflared"

    # ---------- runner plumbing ----------

    def _kubectl(self, ctx: Container):  # type: ignore[no-untyped-def]
        # Late import to avoid a circular dep at module load.
        from ..kubectl_runner import KubectlRunner

        if ctx.kubectl is not None:
            return ctx.kubectl
        import os

        from ..kubeconfig_loader import Kubeconfig, load

        cluster = os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")
        path = (
            ctx.proxmox_k3s_repo
            / "infra"
            / "clusters"
            / cluster
            / "kubeconfig.yaml"
        )
        kubeconfig: Kubeconfig = load(path)
        kubectl = KubectlRunner(kubeconfig=kubeconfig, logger=ctx.logger)
        ctx.kubectl = kubectl
        return kubectl

    def _hostname(self, catalog: dict[str, Any]) -> str:
        ingress = catalog.get("ingress", {})
        base = ingress.get("base_domain", "example.net")
        return f"gitea.{base}"

    # ---------- .env parsing ----------

    @staticmethod
    def _parse_dotenv(text: str) -> dict[str, str]:
        """Best-effort parse of KEY=value lines. Mirrors the
        vaultwarden_k8s_sync._load_dotenv pattern: no
        python-dotenv dep, comments + blanks + quotes handled.
        """
        result: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            result[key] = value
        return result

    def _load_dotenv(self, repo_root: Path) -> dict[str, str]:
        path = repo_root / ".env"
        if not path.exists():
            return {}
        try:
            return self._parse_dotenv(path.read_text())
        except OSError:
            return {}

    @staticmethod
    def _require_env(env: dict[str, str], key: str) -> str:
        value = env.get(key)
        if value is None or not value.strip():
            raise RuntimeError(
                f"missing required .env value {key!r}. "
                f"Set it in .env next to the proxmox-cicd "
                f"repo root or run setup."
            )
        return value.strip()

    # ---------- Cloudflare HTTP wrapper ----------

    def _cf_request(
        self,
        method: str,
        path: str,
        *,
        token_value: str | None = None,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Tiny urllib wrapper for the Cloudflare v4 API.
        Returns the parsed `result` payload (dict or list,
        depending on the endpoint) on success; raises
        RuntimeError with a sanitised message on 4xx/5xx.

        `token_value=None` switches to the legacy
        X-Auth-Email + X-Auth-Key auth header — only used
        for the one-shot token-mint call.
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
            raise RuntimeError(
                f"Cloudflare API {method} {path} failed: "
                f"HTTP {e.code} {e.reason}: {err_body}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cloudflare API {method} {path} "
                f"connection error: {e.reason}"
            ) from e

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Cloudflare API {method} {path} returned "
                f"non-JSON body: {raw[:200]!r}"
            ) from e
        if not payload.get("success", True):
            errs = payload.get("errors", [])
            raise RuntimeError(
                f"Cloudflare API {method} {path} returned "
                f"success=false: {errs}"
            )
        return payload.get("result", payload)

    # ---------- scoped API token ----------

    def _mint_scoped_token(
        self,
        global_api_key: str,
        global_api_email: str,
        account_id: str,
        zone_id: str,
        token_name: str = "cicd-cluster-cloudflared",
    ) -> dict[str, str]:
        """POST /user/tokens — mint a scoped API token
        using the global-API-key + email. The global key
        is read from `.env` and lives only in this call's
        stack frame; nothing on disk references it.

        Returns the same dict the API returns, which
        always includes at least ``id`` and ``value``.
        """
        expires = (
            datetime.utcnow() + timedelta(seconds=_MIN_TOKEN_LIFETIME_S)
        ).isoformat() + "Z"
        body = {
            "name": token_name,
            "policies": [
                {
                    "effect": "allow",
                    "resources": {
                        f"com.cloudflare.api.account.{account_id}": {},
                        f"com.cloudflare.api.account.zone.{zone_id}": {},
                    },
                    "permission_groups": [
                        {"id": _PERM_GROUP_TUNNEL_EDIT[0]},
                        {"id": _PERM_GROUP_DNS_EDIT[0]},
                    ],
                }
            ],
            "expires_on": expires,
            "condition": {"request_ip": ""},
        }
        url = f"{CF_API_BASE}/user/tokens"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "X-Auth-Email": global_api_email,
                "X-Auth-Key": global_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=_CF_API_TIMEOUT_S
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Cloudflare /user/tokens failed: "
                f"HTTP {e.code} {e.reason}: {err_body}"
            ) from e
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Cloudflare /user/tokens returned "
                f"non-JSON body: {raw[:200]!r}"
            ) from e
        if not payload.get("success", True):
            errs = payload.get("errors", [])
            raise RuntimeError(
                f"Cloudflare /user/tokens returned "
                f"success=false: {errs}"
            )
        result = payload.get("result", {})
        return {
            "id": str(result.get("id", "")),
            "value": str(result.get("value", "")),
        }

    def _load_or_mint_token(
        self, ctx: Container, env: dict[str, str]
    ) -> dict[str, str]:
        """Returns {"id", "value"} for the scoped API token.
        Reads `infra/secrets/cloudflared-api-token.json` if
        it exists; otherwise mints a new one using the
        global API key + email from .env and persists it.
        """
        token_path = ctx.repo_root / HOST_TOKEN_FILE
        if token_path.exists():
            try:
                cached = json.loads(token_path.read_text())
                if cached.get("id") and cached.get("value"):
                    ctx.logger.info(
                        "cloudflared.scoped_token_loaded_from_cache",
                        path=str(HOST_TOKEN_FILE),
                        id=cached["id"],
                    )
                    typed: dict[str, str] = {
                        "id": str(cached["id"]),
                        "value": str(cached["value"]),
                    }
                    return typed
            except (json.JSONDecodeError, KeyError) as e:
                ctx.logger.warn(
                    "cloudflared.cached_token_unreadable",
                    path=str(HOST_TOKEN_FILE),
                    error=str(e),
                    resolution="re-minting a new scoped token",
                )

        global_api_key = self._require_env(env, "CLOUDFLARE_GLOBAL_API_KEY")
        global_api_email = self._require_env(env, "CLOUDFLARE_GLOBAL_API_EMAIL")
        account_id = self._require_env(env, "CLOUDFLARE_ACCOUNT_ID")
        zone_id = self._require_env(env, "CLOUDFLARE_ZONE_ID")

        ctx.logger.info(
            "cloudflared.minting_scoped_token",
            account_id=account_id,
            zone_id=zone_id,
            note=(
                "one-shot use of CLOUDFLARE_GLOBAL_API_KEY "
                "(global key) to mint a scoped token; the "
                "global key is not persisted."
            ),
        )
        minted = self._mint_scoped_token(
            global_api_key=global_api_key,
            global_api_email=global_api_email,
            account_id=account_id,
            zone_id=zone_id,
        )

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(minted, indent=2) + "\n")
        os.chmod(token_path, 0o600)
        ctx.logger.info(
            "cloudflared.scoped_token_persisted",
            path=str(HOST_TOKEN_FILE),
            id=minted["id"],
        )
        return minted

    # ---------- tunnel + JWT provisioning ----------

    def _list_tunnels(
        self,
        account_id: str,
        token_value: str,
    ) -> list[dict[str, Any]]:
        result = self._cf_request(
            "GET",
            f"/accounts/{account_id}/cfd_tunnel",
            token_value=token_value,
            query={"name": TUNNEL_NAME, "is_deleted": "false"},
        )
        if isinstance(result, list):
            return list(result)
        tunnels = result.get("tunnels", [])
        return list(tunnels) if isinstance(tunnels, list) else []

    def _create_tunnel(
        self,
        account_id: str,
        token_value: str,
    ) -> dict[str, Any]:
        """POST /accounts/:id/cfd_tunnel. Returns the tunnel
        dict including `id` (UUID), `name`, and the embedded
        `credentials_file` blob.
        """
        body = {
            "name": TUNNEL_NAME,
            "config_src": "cloudflare",
        }
        return self._cf_request(
            "POST",
            f"/accounts/{account_id}/cfd_tunnel",
            token_value=token_value,
            body=body,
        )

    def _ensure_tunnel(
        self,
        ctx: Container,
        account_id: str,
        token_value: str,
    ) -> dict[str, Any]:
        """Look up the tunnel by name; if missing, create it
        (remotely-managed: config_src=cloudflare). Returns the
        tunnel record with `id` populated, the credentials
        blob hydrated, AND the JWT tunnel token attached.
        Persists the full record to
        `infra/secrets/cloudflared-tunnel.json` for the helm
        step + operator audit.
        """
        existing = self._list_tunnels(account_id, token_value)
        if existing:
            tunnel = existing[0]
            ctx.logger.info(
                "cloudflared.tunnel_exists",
                name=TUNNEL_NAME,
                id=tunnel["id"],
            )
        else:
            ctx.logger.info(
                "cloudflared.creating_tunnel",
                name=TUNNEL_NAME,
                account_id=account_id,
            )
            tunnel = self._create_tunnel(account_id, token_value)
            ctx.logger.info(
                "cloudflared.tunnel_created",
                name=TUNNEL_NAME,
                id=tunnel.get("id"),
            )

        if "credentials_file" not in tunnel:
            # The POST response includes the full credentials
            # blob; if we got the tunnel from a GET, we need
            # to fetch it from `token` (compact JSON).
            #
            # NOTE: Cloudflare's /token endpoint sometimes
            # returns a JWT-shaped payload (3 base64 chunks
            # joined by dots), sometimes the compact JSON
            # credentials. We detect by trying JSON.parse
            # first; if it works we treat it as credentials.
            # If it doesn't, the value is the JWT and we
            # carry on (the token is also stored in
            # `tunnel_token` below).
            cred_b64 = self._cf_request(
                "GET",
                f"/accounts/{account_id}/cfd_tunnel/{tunnel['id']}/token",
                token_value=token_value,
            )
            if not isinstance(cred_b64, str):
                # Some API versions return the credentials
                # directly as a dict.
                if isinstance(cred_b64, dict):
                    tunnel["credentials_file"] = cred_b64.get(
                        "credentials_file", cred_b64
                    )
                else:
                    raise RuntimeError(
                        f"GET /cfd_tunnel/{tunnel['id']}/token "
                        f"returned non-string: {cred_b64!r}"
                    )
            else:
                compact = base64.b64decode(cred_b64).decode("utf-8")
                try:
                    compact_obj = json.loads(compact)
                    if isinstance(compact_obj, dict):
                        tunnel["credentials_file"] = {
                            "AccountTag": compact_obj.get("a", account_id),
                            "TunnelSecret": compact_obj.get("s", ""),
                            "TunnelID": compact_obj.get("t", tunnel["id"]),
                        }
                        decoded_jwt = compact
                    else:
                        decoded_jwt = compact
                except (json.JSONDecodeError, ValueError):
                    decoded_jwt = compact
                # Stash the JWT even on the credentials path
                # (best-effort; we never *depend* on it being
                # valid in this branch — the explicit JWT
                # fetch below re-checks).
                if "tunnel_token" not in tunnel:
                    tunnel["tunnel_token"] = decoded_jwt

        tunnel_id = tunnel["id"]
        # Fetch the JWT tunnel token (the bearer-token-shaped
        # string cloudflared accepts via `--token` /
        # `$TUNNEL_TOKEN`). If we already populated
        # `tunnel_token` above and it's a JWT-shape (3
        # base64 chunks joined by dots), skip the extra call.
        if not _looks_like_jwt(tunnel.get("tunnel_token", "")):
            jwt_b64 = self._cf_request(
                "GET",
                f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
                token_value=token_value,
            )
            if isinstance(jwt_b64, dict):
                # Some API versions wrap in a dict; the only
                # useful key is `token`. Everything else is
                # not a JWT.
                jwt_b64 = str(jwt_b64.get("token", ""))
            if not isinstance(jwt_b64, str):
                raise RuntimeError(
                    "Cloudflare /token returned non-string: "
                    f"{jwt_b64!r}"
                )
            try:
                jwt = base64.b64decode(jwt_b64).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                raise RuntimeError(
                    f"Cloudflare /token response is not a "
                    f"base64-decodable JWT: {e}"
                ) from e
            tunnel["tunnel_token"] = jwt

        # Persist on disk (mode 0600).
        tunnel_path = ctx.repo_root / HOST_TUNNEL_FILE
        tunnel_path.parent.mkdir(parents=True, exist_ok=True)
        tunnel_path.write_text(json.dumps(tunnel, indent=2) + "\n")
        os.chmod(tunnel_path, 0o600)
        ctx.logger.info(
            "cloudflared.tunnel_token_persisted",
            path=str(HOST_TUNNEL_FILE),
            id=tunnel_id,
            jwt_len=len(jwt),
        )
        return tunnel

    # ---------- remote ingress provisioning ----------

    def _ensure_remote_ingress(
        self,
        ctx: Container,
        account_id: str,
        token_value: str,
        tunnel_id: str,
        hostname: str,
        upstream_url: str,
    ) -> None:
        """PUT /accounts/:acc/cfd_tunnel/:tun/configurations.

        The remotely-managed chart (`config_src=cloudflare`)
        fetches this config on every tunnel connect; local
        `--config` / `cert.pem` are not consulted. Idempotent
        by construction — last writer wins, and we always
        write the same payload, so re-running is a no-op
        (Cloudflare reports the new `version`).
        """
        payload = {
            "config": {
                "ingress": [
                    {"hostname": hostname, "service": upstream_url},
                    {"service": "http_status:404"},
                ],
            }
        }
        result = self._cf_request(
            "PUT",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            token_value=token_value,
            body=payload,
        )
        ctx.logger.info(
            "cloudflared.ingress_pushed",
            tunnel_id=tunnel_id,
            version=result.get("version"),
            source=result.get("source"),
            rule=f"{hostname} -> {upstream_url}",
        )

    # ---------- DNS provisioning ----------

    def _ensure_dns_cname(
        self,
        ctx: Container,
        zone_id: str,
        token_value: str,
        hostname: str,
        tunnel_id: str,
    ) -> None:
        """Idempotent CNAME upsert for `<hostname> ->
        <tunnel_id>.cfargotunnel.com` (proxied). The proxy
        flag (orange-cloud) is what makes Cloudflare accept
        the tunnel target; without it the record is treated
        as grey-clouded DNS-only and won't tunnel.
        """
        record_type = "CNAME"
        target = f"{tunnel_id}.cfargotunnel.com"

        existing = self._cf_request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            token_value=token_value,
            query={"type": record_type, "name": hostname},
        )
        records = (
            existing if isinstance(existing, list) else existing.get("result", [])
        )
        if records:
            rec = records[0]
            ctx.logger.info(
                "cloudflared.dns_record_exists",
                hostname=hostname,
                target=rec.get("content"),
                proxied=rec.get("proxied"),
            )
            if rec.get("content") != target or rec.get("proxied") is not True:
                self._cf_request(
                    "PUT",
                    f"/zones/{zone_id}/dns_records/{rec['id']}",
                    token_value=token_value,
                    body={
                        "type": record_type,
                        "name": hostname,
                        "content": target,
                        "proxied": True,
                        "comment": "managed by proxmox-cicd cloudflared app",
                    },
                )
                ctx.logger.info(
                    "cloudflared.dns_record_updated",
                    hostname=hostname,
                    target=target,
                )
            return

        self._cf_request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            token_value=token_value,
            body={
                "type": record_type,
                "name": hostname,
                "content": target,
                "proxied": True,
                "comment": "managed by proxmox-cicd cloudflared app",
            },
        )
        ctx.logger.info(
            "cloudflared.dns_record_created",
            hostname=hostname,
            target=target,
            proxied=True,
        )

    # ---------- Envoy upstream discovery ----------

    def _envoy_service_for(
        self, ctx: Container, gateway_namespace: str, gateway_name: str
    ) -> tuple[str, str]:
        """Resolve the Envoy proxy Service that fronts a given
        Gateway. The chart-bundled envoy-gateway controller
        creates a `<gateway-name>-<hash>` Service in
        `envoy-gateway-system` (the controller namespace),
        labeled with `gateway.envoyproxy.io/owning-gateway-name`
        + `gateway.envoyproxy.io/owning-gateway-namespace`.
        Returns (namespace, name).
        """
        kubectl = self._kubectl(ctx)
        selector = (
            f"{_ENVOY_GW_LABEL_NAME}={gateway_name},"
            f"{_ENVOY_GW_LABEL_NAMESPACE}={gateway_namespace}"
        )
        out = kubectl.get(
            resource="svc",
            namespace=_ENVOY_GW_NAMESPACE,
            label_selector=selector,
            jsonpath="{.items[0].metadata.name}",
        )
        if out.returncode != 0 or not (out.stdout or "").strip():
            raise RuntimeError(
                f"could not find Envoy Service for gateway "
                f"{gateway_namespace}/{gateway_name}; "
                f"label_selector={selector}. Has the Gateway "
                f"been Programmed yet? "
                f"kubectl -n {_ENVOY_GW_NAMESPACE} get svc -l "
                f"{selector}"
            )
        svc_name = (out.stdout or "").strip()
        return _ENVOY_GW_NAMESPACE, svc_name

    # ---------- Vaultwarden sync-side note ----------

    def _seed_vaultwarden_note(
        self,
        ctx: Container,
        jwt: str,
    ) -> None:
        """Push the JWT tunnel token to Vaultwarden as a
        Secure Note so VaultwardenK8sSync recreates the
        chart-managed `cloudflare-tunnel-remote` Secret if
        helm ever deletes it (e.g. on tofu destroy + apply).

        Re-runs are no-op (the script is idempotent on
        app+namespace+secret-name+secret-key).

        Failure is non-fatal — the helm install still owns
        the Secret at apply-time, and VKS will pick the
        note up within one sync interval (~5 min) after
        destroy.
        """
        try:
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "scripts/vaultwarden-seed-note.py",
                    "--app", VWS_NOTE_APP,
                    "--namespace", VWS_NOTE_NAMESPACE,
                    "--secret-name", VWS_NOTE_SECRET_NAME,
                    "--secret-key", VWS_NOTE_SECRET_KEY,
                    "--body", jwt,
                    "--password-file", "/tmp/vw.pw",
                ],
                cwd=str(ctx.repo_root),
                capture_output=True,
                text=True,
                timeout=60.0,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            ctx.logger.warn(
                "cloudflared.vws_seed_unavailable",
                error=str(e),
                resolution=(
                    "JWT still cached in infra/secrets; "
                    "cloudflared runs without VKS sync."
                ),
            )
            return

        if result.returncode != 0:
            ctx.logger.warn(
                "cloudflared.vws_seed_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip()[:500],
                stdout=result.stdout.strip()[-500:],
                resolution=(
                    "re-run scripts/vaultwarden-seed-note.py "
                    "manually to push the JWT."
                ),
            )
            return

        ctx.logger.info(
            "cloudflared.vws_seed_ok",
            app=VWS_NOTE_APP,
            namespace=VWS_NOTE_NAMESPACE,
            secret_name=VWS_NOTE_SECRET_NAME,
            secret_key=VWS_NOTE_SECRET_KEY,
        )

    # ---------- plan / apply / status / destroy ----------

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        host = self._hostname(catalog)
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm upgrade --install {HELM_RELEASE_NAME} "
                f"{CHART_TGZ} --version {CHART_VERSION} "
                f"-n {NAMESPACE} --create-namespace "
                f"--set cloudflare.tunnel_token=$JWT "
                f"--set image.tag={APP_VERSION} "
                f"--set replicaCount=1",
            ],
            would_apply=[
                f"Cloudflare Tunnel {TUNNEL_NAME} "
                f"(POST /accounts/:id/cfd_tunnel if absent; "
                f"config_src=cloudflare)",
                f"Remote-ingress rule on the tunnel "
                f"(PUT .../configurations: "
                f"{host} -> http://<envoy-svc>:80 + "
                f"http_status:404 catch-all)",
                f"DNS CNAME {host} -> <tunnel-uuid>"
                f".cfargotunnel.com (proxied, on zone from "
                f"CLOUDFLARE_ZONE_ID)",
                f"Vaultwarden Secure Note "
                f"({VWS_NOTE_APP}/{VWS_NOTE_NAMESPACE}/"
                f"{VWS_NOTE_SECRET_NAME}/{VWS_NOTE_SECRET_KEY}) "
                f"pushing the JWT so VaultwardenK8sSync "
                f"recreates the chart's Secret on destroy+apply",
                f"Scoped API token `cicd-cluster-cloudflared` "
                f"(POST /user/tokens on first apply; cached at "
                f"{HOST_TOKEN_FILE})",
            ],
            notes=[
                f"image: cloudflare/cloudflared:{APP_VERSION}",
                f"upstream: Envoy Gateway Service in "
                f"{_ENVOY_GW_NAMESPACE} (labels "
                f"{_ENVOY_GW_LABEL_NAME}={DEFAULT_GATEWAY_NAME} "
                f"+ {_ENVOY_GW_LABEL_NAMESPACE}="
                f"{DEFAULT_GATEWAY_NAMESPACE})",
                f"public hostname: https://{host}",
                (
                    "credentials: scoped API token (mint-once, "
                    f"cached at {HOST_TOKEN_FILE}); tunnel + "
                    f"JWT cached at {HOST_TUNNEL_FILE}. The "
                    f"chart-managed Secret "
                    f"`{VWS_NOTE_SECRET_NAME}` (key "
                    f"`{VWS_NOTE_SECRET_KEY}`) is reseeded by "
                    f"VaultwardenK8sSync on destroy+apply."
                ),
                (
                    "scope: Tunnel:Edit + DNS:Edit on the "
                    "configured zone only; global API key "
                    "used exactly once"
                ),
                (
                    "chart: cloudflare-tunnel-remote @ "
                    f"{CHART_VERSION} (vendored at {CHART_TGZ}; "
                    f"remotely-managed — config lives in the "
                    f"Cloudflare dashboard, not in a local "
                    f"`config.yaml` mount)"
                ),
            ],
        )

    def apply(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppApplyResult:
        kubectl = self._kubectl(ctx)
        env = self._load_dotenv(ctx.repo_root)
        account_id = self._require_env(env, "CLOUDFLARE_ACCOUNT_ID")
        zone_id = self._require_env(env, "CLOUDFLARE_ZONE_ID")
        domain = self._require_env(env, "CLOUDFLARE_DOMAIN")
        hostname = self._hostname(catalog)
        if not hostname.endswith("." + domain):
            raise RuntimeError(
                f"computed hostname {hostname!r} does not match "
                f"the configured zone {domain!r}; check "
                f"catalog.ingress.base_domain"
            )

        # 0. Pre-create the namespace. helm install
        #    --create-namespace handles it too, but we want
        #    a deterministic name on the first install so
        #    subsequent steps can rely on it.
        ns_create = kubectl.apply(
            manifest=(
                "apiVersion: v1\n"
                "kind: Namespace\n"
                "metadata:\n"
                f"  name: {NAMESPACE}\n"
                "  labels:\n"
                '    app.kubernetes.io/name: cloudflared\n'
            ),
            namespace=None,
            server_side=False,
        )
        if ns_create.returncode != 0:
            raise RuntimeError(
                f"kubectl apply Namespace={NAMESPACE} failed: "
                f"rc={ns_create.returncode} "
                f"stderr={ns_create.stderr.strip()[:500]}"
            )

        # 1. Load or mint the scoped API token.
        token = self._load_or_mint_token(ctx, env)

        # 2. Ensure the tunnel + JWT exist on Cloudflare.
        tunnel = self._ensure_tunnel(
            ctx,
            account_id=account_id,
            token_value=token["value"],
        )
        tunnel_id = tunnel["id"]
        jwt = tunnel["tunnel_token"]

        # 3. Resolve the Envoy Gateway Service.
        gw_ns, gw_svc = self._envoy_service_for(
            ctx,
            gateway_namespace=DEFAULT_GATEWAY_NAMESPACE,
            gateway_name=DEFAULT_GATEWAY_NAME,
        )
        upstream_url = f"http://{gw_svc}.{gw_ns}.svc.cluster.local:80"
        ctx.logger.info(
            "cloudflared.upstream_resolved",
            gateway_namespace=gw_ns,
            gateway_service=gw_svc,
            hostname=hostname,
            upstream=upstream_url,
        )

        # 4. Push the remote-ingress rule to Cloudflare
        #    (the chart re-fetches it on every connection).
        self._ensure_remote_ingress(
            ctx,
            account_id=account_id,
            token_value=token["value"],
            tunnel_id=tunnel_id,
            hostname=hostname,
            upstream_url=upstream_url,
        )

        # 5. Ensure the DNS record exists and is proxied.
        self._ensure_dns_cname(
            ctx,
            zone_id=zone_id,
            token_value=token["value"],
            hostname=hostname,
            tunnel_id=tunnel_id,
        )

        # 6. Seed the JWT into Vaultwarden so VKS can
        #    recreate the chart-managed Secret after a
        #    destroy. Failure is non-fatal here.
        self._seed_vaultwarden_note(ctx, jwt)

        # 7. helm install / upgrade against the vendored
        #    upstream chart. We pass the .tgz path
        #    directly; helm reads Chart.yaml + templates/
        #    out of the archive.
        chart_path = ctx.repo_root / CHART_TGZ
        if not chart_path.exists():
            raise RuntimeError(
                f"vendored chart tgz not found at "
                f"{chart_path.relative_to(ctx.repo_root)}; "
                f"did you forget to git pull?"
            )

        # Render the tunnel_token into a YAML file rather
        # than passing it via `--set-string`. The
        # Cloudflare /token endpoint returns either a
        # base64-wrapped compact JSON `{a,t,s}` (when the
        # tunnel was created with `config_src=cloudflare`)
        # or a JWT `a.b.c` triple. Both forms contain
        # characters that helm's YAML value parser
        # detects as a flow-mapping (`{...}`) when double-
        # quoted, which propagates into secret.yaml's
        # `stringData: tunnelToken:` and is rejected by
        # the API server with `expected string, got map`.
        #
        # The fix: use a YAML **single-quoted** string
        # (`'foo {bar}'`), which YAML treats as a literal
        # scalar regardless of the contents. Inside the
        # quoted string, single quotes are doubled per
        # YAML 1.2 quoting rules.
        rendered_values = ctx.repo_root / "values" / "cloudflared-tunnel-remote.values-rendered.yaml"
        rendered_values.parent.mkdir(parents=True, exist_ok=True)
        token_quoted = "'" + jwt.replace("'", "''") + "'"
        rendered_values.write_text(
            "# Auto-generated by the cloudflared app at "
            "apply-time. Do not edit — re-runs overwrite. "
            "Gitignored via values/*.values-rendered.yaml.\n"
            f"cloudflare:\n"
            f"  tunnel_token: {token_quoted}\n"
            f"image:\n"
            f"  tag: '{APP_VERSION}'\n"
            f"replicaCount: 1\n"
        )
        os.chmod(rendered_values, 0o600)

        result = ctx.helm.install_or_upgrade(
            release=HELM_RELEASE_NAME,
            chart=str(chart_path),
            namespace=NAMESPACE,
            version=CHART_VERSION,
            values_files=(rendered_values,),
            extra_args=(
                # VaultwardenK8sSync pre-creates the chart's
                # Secret on the first apply (it's the same
                # Secret whose key the orchestrator's note
                # describes: cloudflare-tunnel-remote /
                # tunnelToken). On the first helm upgrade,
                # the chart refuses to adopt a Secret that's
                # owned by another tool. --take-ownership
                # lets helm add its annotations and labels
                # and treat the Secret as its own.
                "--take-ownership",
            ),
            timeout_s=180.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"helm install cloudflare-tunnel-remote "
                f"failed: rc={result.returncode} stderr="
                f"{result.stderr.strip()[:500]}"
            )
        ctx.logger.info(
            "cloudflared.helm_install_ok",
            release=HELM_RELEASE_NAME,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
        )

        # 8. Wait for the cloudflared Deployment to be Ready.
        wait = kubectl.wait_deployments_available(
            namespace=NAMESPACE,
            label_selector="pod=cloudflared",
            timeout_s=120.0,
        )
        if wait.returncode != 0:
            ctx.logger.warn(
                "cloudflared.deployment_not_ready",
                stderr=wait.stderr.strip()[:500],
            )

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=HELM_RELEASE_NAME,
            chart_version=CHART_VERSION,
            image_version=APP_VERSION,
            ingress_host=hostname,
            next_step=None,
        )

    def status(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppStatus:
        list_release = ctx.helm.list_releases(namespace=NAMESPACE)
        release_present = list_release.returncode == 0 and bool(
            (list_release.stdout or "").strip()
        )
        notes: list[str] = []
        token_path = ctx.repo_root / HOST_TOKEN_FILE
        tunnel_path = ctx.repo_root / HOST_TUNNEL_FILE
        if token_path.exists():
            notes.append(f"scoped API token cached: {HOST_TOKEN_FILE}")
        else:
            notes.append("scoped API token NOT yet minted")
        if tunnel_path.exists():
            try:
                jwt_present = bool(
                    json.loads(tunnel_path.read_text()).get("tunnel_token")
                )
            except (json.JSONDecodeError, OSError):
                jwt_present = False
            notes.append(
                f"tunnel credentials cached: {HOST_TUNNEL_FILE}"
                + (" (with JWT)" if jwt_present else " (no JWT)")
            )
        else:
            notes.append("tunnel credentials NOT yet provisioned")
        return AppStatus(
            app_name=self.name,
            namespace=NAMESPACE,
            release_present=release_present,
            chart_version=CHART_VERSION if release_present else None,
            image_version=APP_VERSION if release_present else None,
            ingress_host=self._hostname(catalog) if release_present else None,
            notes=notes,
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        # We do NOT delete the Cloudflare tunnel or DNS
        # record here — they're durable Cloudflare-side
        # resources that cost nothing to keep and are
        # useful to inspect after teardown. The operator
        # can `cloudflared tunnel delete` them manually if
        # desired (the cloudflared CLI is not part of this
        # repo's bootstrap — install from
        # https://pkg.cloudflare.com/).
        result = ctx.helm.uninstall(HELM_RELEASE_NAME, NAMESPACE, timeout_s=180.0)
        if result.returncode != 0:
            ctx.logger.warn(
                "cloudflared.helm_uninstall_failed",
                release=HELM_RELEASE_NAME,
                stderr=result.stderr.strip()[:500],
            )
        kubectl = self._kubectl(ctx)
        kubectl.delete_namespace(NAMESPACE)


register(CloudflaredApp)
