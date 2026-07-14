"""Tests for the cloudflared app — remotely-managed Cloudflare
Tunnel via the upstream cloudflare-tunnel-remote-0.1.2
chart. Mocks the Cloudflare API + helm + kubectl +
Vaultwarden subprocess so the unit tests don't need a real
account or cluster.

What we lock down:

  - .env parsing (.parse_dotenv, _load_dotenv, _require_env)
  - Registry: app is registered under "cloudflared"
  - plan(): references the vendored .tgz, the upstream chart
    version, the JWT-bearing Vaultwarden note, and the
    remote-config PUT path
  - apply() end-to-end on a fresh account:
      * GET /accounts/{acc}/cfd_tunnel -> []
      * POST /accounts/{acc}/cfd_tunnel (create remote tunnel)
      * GET /accounts/{acc}/cfd_tunnel/{tun}/token (JWT)
      * PUT /accounts/{acc}/cfd_tunnel/{tun}/configurations
      * GET /zones/{zone}/dns_records -> []
      * POST /zones/{zone}/dns_records
      * subprocess.run with the right VWS note args
      * helm install with vendored chart + JWT via --set
      * JWT persisted (decoded, mode 0600)
  - apply() on a re-run with existing tunnel:
      * no POST /cfd_tunnel, no DNS write — ingress PUT only
  - apply() on a re-run with drifted DNS record:
      * PUT /zones/{zone}/dns_records/{rec_id} with
        proxied=True (auto-corrects drift)
  - apply() failure paths:
      * ingress PUT returning 400 -> RuntimeError surfaces
      * vaultwarden-seed-note.py failing -> non-fatal
        warning, helm install still completes
  - destroy(): helm uninstall + namespace delete
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps import app_by_name, reset_registry
from provisioner.lib.apps.cloudflared import (
    APP_VERSION,
    CHART_TGZ,
    CHART_VERSION,
    HELM_RELEASE_NAME,
    NAMESPACE,
    TUNNEL_NAME,
    VWS_NOTE_APP,
    VWS_NOTE_NAMESPACE,
    VWS_NOTE_SECRET_KEY,
    VWS_NOTE_SECRET_NAME,
    CloudflaredApp,
)
from provisioner.lib.container import Container


# ----- constants used by both the orchestrator and the mocks -----

JWT_PLAINTEXT = (
    "eyJhIjoiMmU5YzA5YjI3ZDJhMDg5YzUzMWIxMmFlMGYwZTZmZjMiLCJ0Ijoi"
    "Y2JhYTg4MDctZDM1OS00MWQ2LWJhYjktMTkwNjJlNzUyNzRjIiwicyI6Ik84"
    "bWF6VkxMYzliK0ljSWVaUlZPMWRxZEpCYjY0aG5pdDByT3NOZlQ4amtCcDY1"
    "Q3hFczh2a3BJamtwSHhsNk1LUzZ3ZitSYW5ueWNSdUE3TlpTdG1nPT0ifQ=="
)
JWT_B64 = base64.b64encode(JWT_PLAINTEXT.encode("utf-8")).decode("ascii")
TUNNEL_UUID = "cbaa8807-d359-41d6-bab9-19062e75274c"
ACCOUNT = "2e9c09b27d2a089c531b12ae0f0e6ff3"
ZONE = "deadbeefcafe"
HOSTNAME = "gitea.bruj0.net"
UPSTREAM = (
    "http://envoy-gitea-gitea-83aba4b0.envoy-gateway-system"
    ".svc.cluster.local:80"
)
ENVOY_SVC_NAME = "envoy-gitea-gitea-83aba4b0"


# ----- urlopen fixture: emulate Cloudflare v4 with a route table -----


class _Resp:
    """Tiny urllib response-like object."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def _cf_response(payload: object) -> bytes:
    return json.dumps(
        {"success": True, "result": payload, "errors": []}
    ).encode("utf-8")


def _failing_response(message: str, status: int = 400) -> bytes:
    return json.dumps(
        {
            "success": False,
            "errors": [{"code": status, "message": message}],
        }
    ).encode("utf-8")


def _normalize_path(url: str) -> str:
    """Return only the path component of a URL, dropping
    scheme + host + query string. Accepts full URLs
    ("https://api.../client/v4/accounts/{acc}/cfd_tunnel?
    name=...") or already-bare paths.
    """
    # First strip the query string so we don't keep it in
    # the final path.
    url = url.split("?", 1)[0]
    if "://" not in url:
        return url
    tail = url.split("://", 1)[1]
    if "/" not in tail:
        return "/"
    return "/" + tail.split("/", 1)[1]  # type: ignore[no-any-return]  # pyright


def _make_urlopen(handlers: dict[tuple[str, str], object]) -> MagicMock:
    """Return a MagicMock standing in for urllib.request.urlopen.

    `handlers` is keyed by `(METHOD, prefix)`; longest-prefix
    match wins per method, so `(POST, /accounts/{acc}/cfd_tunnel)`
    can return a tunnel dict while `(GET, /accounts/{acc}/cfd_tunnel)`
    returns a list. /cfd_tunnel/{id}/token beats /cfd_tunnel
    when both have the same prefix length.
    """

    def _fake(req: object, timeout: float = 30.0, context: object = None) -> _Resp:
        method = getattr(req, "method", "GET")
        url = getattr(req, "full_url", str(req))
        path = _normalize_path(url)
        # Filter to handlers for this method, then longest-prefix match.
        candidates = [
            (prefix, payload)
            for (m, prefix), payload in handlers.items()
            if m == method and (path == prefix or path.startswith(prefix + "/"))
        ]
        for _prefix, payload in sorted(candidates, key=lambda kv: len(kv[0]), reverse=True):
            if callable(payload):
                payload = payload(req)
            if isinstance(payload, BaseException):
                raise payload
            if isinstance(payload, bytes):
                return _Resp(payload)
            return _Resp(_cf_response(payload))
        return _Resp(_cf_response({"success": False, "errors": [{"code": 404}]}))

    return MagicMock(side_effect=_fake)


# ----- ctx helpers -----


def _build_ctx(tmp_path: Path) -> Container:
    """Build a Container with the project's standard fixtures, plus
    a vendored chart .tgz at the path CloudflaredApp looks up.
    """
    repo = tmp_path
    (repo / ".env").write_text(
        "\n".join(
            [
                "# top comment",
                f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}",
                f'CLOUDFLARE_ZONE_ID="{ZONE}"',
                "CLOUDFLARE_DOMAIN=bruj0.net",
                "CLOUDFLARE_GLOBAL_API_KEY=global-key-not-persisted",
                "CLOUDFLARE_GLOBAL_API_EMAIL=secrets@bruj0.net",
                "",
            ]
        )
    )
    (repo / "infra" / "clusters" / "cicd").mkdir(parents=True)
    (repo / "infra" / "clusters" / "cicd" / "kubeconfig.yaml").write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster: {server: https://10.0.0.64:6443}\n"
        "  name: cicd\ncontexts:\n- context: {cluster: cicd, user: cicd}\n"
        "  name: cicd\ncurrent-context: cicd\n"
        "users:\n- name: cicd\n  user: {token: t}\n"
    )
    (repo / "values").mkdir(exist_ok=True)
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "infra" / "secrets").mkdir(parents=True, exist_ok=True)

    # Vendored chart .tgz (the orchestrator asserts it exists).
    src = Path("/home/bruj0/projects/proxmox/proxmox-cicd") / CHART_TGZ
    dest = repo / CHART_TGZ
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())

    ctx = Container.for_tests(
        proxmox_k3s_repo=repo,
        repo_root=repo,
        audit_log=repo / "logs" / "test_cloudflared.audit.jsonl",
    )

    # helm mock
    helm = MagicMock()
    helm.install_or_upgrade.return_value = MagicMock(
        returncode=0, stdout="installed", stderr=""
    )
    helm.uninstall.return_value = MagicMock(
        returncode=0, stdout="uninstalled", stderr=""
    )
    helm.list_releases.return_value = MagicMock(
        returncode=0,
        stdout="RELEASE\tNAMESPACE\tSTATUS\n",
        stderr="",
    )
    ctx.helm = helm

    # kubectl mock
    kubectl = MagicMock()
    kubectl.apply.return_value = MagicMock(returncode=0, stdout="", stderr="")
    kubectl.get.return_value = MagicMock(
        returncode=0, stdout=ENVOY_SVC_NAME, stderr=""
    )
    kubectl.wait_deployments_available.return_value = MagicMock(
        returncode=0, stdout="", stderr=""
    )
    kubectl.delete_namespace.return_value = MagicMock(
        returncode=0, stdout="", stderr=""
    )
    ctx.kubectl = kubectl

    # orchestrator -> None (no orchestrator methods used by apply)
    return ctx


# ----- registry mechanics -----


def test_cloudflared_is_registered() -> None:
    reset_registry()
    import importlib

    from provisioner.lib.apps import cloudflared as cf_mod

    importlib.reload(cf_mod)
    assert app_by_name("cloudflared") is cf_mod.CloudflaredApp


# ----- .env parsing -----


def test_parse_dotenv_handles_comments_blanks_and_quotes() -> None:
    text = "\n".join(
        [
            "# top comment",
            "FOO=bar",
            "BAZ='qux qux'",
            'NUM=42',
            "",
            "  EMPTY=",
            "MALFORMED",
        ]
    )
    parsed = CloudflaredApp._parse_dotenv(text)
    assert parsed == {"FOO": "bar", "BAZ": "qux qux", "NUM": "42", "EMPTY": ""}


def test_require_env_raises_when_missing() -> None:
    with pytest.raises(RuntimeError) as ei:
        CloudflaredApp._require_env({}, "CF_MISSING")
    assert "CF_MISSING" in str(ei.value)


# ----- plan() -----


def test_plan_references_vendored_chart_and_remote_ingress(
    tmp_path: Path,
) -> None:
    ctx = _build_ctx(tmp_path)
    out = CloudflaredApp().plan(ctx, {"ingress": {"base_domain": "bruj0.net"}})
    text = " ".join(out.would_install + out.would_apply + out.notes)
    assert str(CHART_TGZ) in text, "plan must reference the vendored chart path"
    assert HELM_RELEASE_NAME in text
    assert CHART_VERSION in text
    assert APP_VERSION in text
    assert "/configurations" in text, "plan must mention the remote-ingress PUT"
    assert "config_src=cloudflare" in text, "plan must mark the tunnel as remote"
    assert "JWT" in text or "tunnel_token" in text, "plan must mention the JWT"
    assert "cfargotunnel.com" in text
    assert VWS_NOTE_SECRET_NAME in text
    assert VWS_NOTE_SECRET_KEY in text


# ----- apply() on a fresh account -----


def _cf_handlers_for_fresh_tunnel() -> dict[tuple[str, str], object]:
    """Cloudflare API call sequence for a fresh apply.

    Order:
      1. GET  /accounts/{acc}/cfd_tunnel              -> []
      2. POST /accounts/{acc}/cfd_tunnel              -> {id, ...}
      3. GET  /accounts/{acc}/cfd_tunnel/{tun}/token  -> base64(JWT)
      4. PUT  /accounts/{acc}/cfd_tunnel/{tun}/configurations
      5. GET  /zones/{zone}/dns_records              -> []
      6. POST /zones/{zone}/dns_records
    """
    cf_payload = {
        "tunnel_id": TUNNEL_UUID,
        "version": 1,
        "config": {"ingress": []},
        "source": "cloudflare",
    }
    return {
        ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): [],
        ("POST", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): {
            "id": TUNNEL_UUID,
            "name": TUNNEL_NAME,
            "config_src": "cloudflare",
            "credentials_file": {
                "AccountTag": ACCOUNT,
                "TunnelID": TUNNEL_UUID,
                "TunnelSecret": "x",
            },
        },
        ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/token"): JWT_B64,
        (
            "PUT",
            f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/configurations",
        ): cf_payload,
        ("GET", f"/client/v4/zones/{ZONE}/dns_records"): [],
        ("POST", f"/client/v4/zones/{ZONE}/dns_records"): {
            "id": "rec-new",
            "type": "CNAME",
            "name": HOSTNAME,
            "content": f"{TUNNEL_UUID}.cfargotunnel.com",
            "proxied": True,
        },
    }


def test_apply_fresh_run_calls_cf_and_helm_in_order(tmp_path: Path) -> None:
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)
    captured: list[tuple[str, str, dict[str, object] | None]] = []

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        method = getattr(req, "method", "GET")
        url = getattr(req, "full_url", str(req))
        body = getattr(req, "data", None)
        parsed: dict[str, object] | None = None
        if body:
            try:
                parsed = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
        captured.append((method, url, parsed))
        return _make_urlopen(_cf_handlers_for_fresh_tunnel())(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0, stdout="seeded", stderr="")
        result = CloudflaredApp().apply(
            ctx, {"ingress": {"base_domain": "bruj0.net"}}
        )

    # 1. Result shape.
    assert result.app_name == "cloudflared"
    assert result.namespace == NAMESPACE
    assert result.release == HELM_RELEASE_NAME
    assert result.chart_version == CHART_VERSION
    assert result.image_version == APP_VERSION
    assert result.ingress_host == HOSTNAME
    assert result.next_step is None

    # 2. CF API sequence — at least these calls happened.
    cf_bare = [(m, u.split("?", 1)[0], b) for (m, u, b) in captured]
    # No tunnel existed -> POST /cfd_tunnel was issued.
    posts_tunnel = [
        c for c in cf_bare if c[0] == "POST" and c[1].endswith("/cfd_tunnel")
    ]
    assert len(posts_tunnel) == 1
    # Tunnel JWT was fetched.
    gets_token = [
        c
        for c in cf_bare
        if c[0] == "GET" and c[1].endswith(f"/cfd_tunnel/{TUNNEL_UUID}/token")
    ]
    assert len(gets_token) == 1
    # Remote ingress PUT.
    put_ingress = [
        c
        for c in cf_bare
        if c[0] == "PUT" and c[1].endswith("/configurations")
    ]
    assert len(put_ingress) == 1
    _m, _u, ingress_body = put_ingress[0]
    assert ingress_body is not None
    assert ingress_body["config"]["ingress"][0]["hostname"] == HOSTNAME
    assert ingress_body["config"]["ingress"][0]["service"] == UPSTREAM
    assert ingress_body["config"]["ingress"][1] == {"service": "http_status:404"}
    # DNS: GET + POST (no existing record).
    dns_posts = [c for c in cf_bare if c[0] == "POST" and c[1].endswith("/dns_records")]
    assert len(dns_posts) == 1
    _m, _u, dns_body = dns_posts[0]
    assert dns_body["type"] == "CNAME"
    assert dns_body["name"] == HOSTNAME
    assert dns_body["content"] == f"{TUNNEL_UUID}.cfargotunnel.com"
    assert dns_body["proxied"] is True

    # 3. Tunnel + JWT persisted, mode 0600.
    on_disk = json.loads(
        (tmp_path / "infra" / "secrets" / "cloudflared-tunnel.json").read_text()
    )
    assert on_disk["id"] == TUNNEL_UUID
    # JWT must be the decoded plaintext, NOT the base64.
    assert on_disk["tunnel_token"] == JWT_PLAINTEXT
    secret_path = tmp_path / "infra" / "secrets" / "cloudflared-tunnel.json"
    assert secret_path.stat().st_mode & 0o777 == 0o600

    # 4. Helm install — vendored chart + values file
    #    carrying the JWT. We don't pass the JWT via
    #    --set (which would route through helm's
    #    YAML/JSON value parser and detect
    #    `{a:"...",t:"...",s:"..."}` as a flow-mapping).
    assert ctx.helm.install_or_upgrade.called
    helm_args = ctx.helm.install_or_upgrade.call_args
    helm_kwargs = helm_args.kwargs
    assert helm_kwargs["release"] == HELM_RELEASE_NAME
    assert helm_kwargs["namespace"] == NAMESPACE
    assert helm_kwargs["version"] == CHART_VERSION
    assert helm_kwargs["chart"] == str(tmp_path / CHART_TGZ)
    values_files = helm_kwargs["values_files"]
    assert len(values_files) == 1
    rendered = Path(values_files[0])
    rendered_text = rendered.read_text()
    # Single-quoted YAML literal — keeps flow mappings (e.g.
    # {a,t,s} compact-JSON tokens) as a string scalar instead
    # of a YAML map.
    assert f"tunnel_token: '{JWT_PLAINTEXT}'" in rendered_text
    assert f"tag: '{APP_VERSION}'" in rendered_text
    assert rendered_text.count("replicaCount: 1") == 1
    # Mode 0600 on the values file.
    assert rendered.stat().st_mode & 0o777 == 0o600

    # 5. Namespace pre-create + Envoy lookup happened.
    assert ctx.kubectl.apply.called
    assert ctx.kubectl.get.called

    # 6. Vaultwarden seed invoked with the right shape.
    assert mock_subproc.called
    cmd = mock_subproc.call_args.args[0]
    assert "--app" in cmd and VWS_NOTE_APP in cmd
    assert VWS_NOTE_NAMESPACE in cmd
    assert VWS_NOTE_SECRET_NAME in cmd
    assert VWS_NOTE_SECRET_KEY in cmd
    body_idx = cmd.index("--body") + 1
    assert cmd[body_idx] == JWT_PLAINTEXT


# ----- apply() on a re-run with existing tunnel -----


def test_apply_existing_tunnel_is_noop_for_dns_and_tunnel_create(
    tmp_path: Path,
) -> None:
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)
    (tmp_path / "infra" / "secrets" / "cloudflared-api-token.json").write_text(
        json.dumps({"id": "scoped-tok-id", "value": "scoped-tok-value"})
    )
    (tmp_path / "infra" / "secrets" / "cloudflared-tunnel.json").write_text(
        json.dumps(
            {
                "id": TUNNEL_UUID,
                "name": TUNNEL_NAME,
                "tunnel_token": "old-jwt",
                "credentials_file": {
                    "AccountTag": ACCOUNT,
                    "TunnelID": TUNNEL_UUID,
                    "TunnelSecret": "x",
                },
            }
        )
    )

    captured: list[tuple[str, str, dict[str, object] | None]] = []

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        method = getattr(req, "method", "GET")
        url = getattr(req, "full_url", str(req))
        body = getattr(req, "data", None)
        parsed: dict[str, object] | None = None
        if body:
            try:
                parsed = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
        captured.append((method, url, parsed))
        handlers = {
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): [
                {"id": TUNNEL_UUID, "name": TUNNEL_NAME}
            ],
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/token"): JWT_B64,
            (
                "PUT",
                f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/configurations",
            ): {
                "tunnel_id": TUNNEL_UUID,
                "version": 2,
                "config": {"ingress": []},
                "source": "cloudflare",
            },
            ("GET", f"/client/v4/zones/{ZONE}/dns_records"): [
                {
                    "id": "rec1",
                    "type": "CNAME",
                    "name": HOSTNAME,
                    "content": f"{TUNNEL_UUID}.cfargotunnel.com",
                    "proxied": True,
                }
            ],
        }
        return _make_urlopen(handlers)(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0)
        CloudflaredApp().apply(ctx, {"ingress": {"base_domain": "bruj0.net"}})

    bare = [(m, u.split("?", 1)[0]) for (m, u, _b) in captured]
    # No POST /cfd_tunnel on a re-run.
    assert not [
        m for m in bare if m[0] == "POST" and m[1].endswith("/cfd_tunnel")
    ]
    # DNS already correct -> no DNS writes of any kind.
    dns_writes = [
        m
        for m in bare
        if m[0] in ("POST", "PUT") and m[1].endswith("/dns_records")
    ]
    assert dns_writes == []
    # Ingress PUT still runs every apply (idempotent at CF layer).
    assert any(m for m in bare if m[0] == "PUT" and m[1].endswith("/configurations"))


# ----- apply() on a re-run with drifted DNS record -----


def test_apply_dns_patch_when_existing_record_drifted(tmp_path: Path) -> None:
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)
    (tmp_path / "infra" / "secrets" / "cloudflared-api-token.json").write_text(
        json.dumps({"id": "tid", "value": "tv"})
    )
    captured: list[tuple[str, str, dict[str, object] | None]] = []

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        method = getattr(req, "method", "GET")
        url = getattr(req, "full_url", str(req))
        body = getattr(req, "data", None)
        parsed: dict[str, object] | None = None
        if body:
            parsed = json.loads(body)
        captured.append((method, url, parsed))
        handlers = {
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): [
                {"id": TUNNEL_UUID, "name": TUNNEL_NAME}
            ],
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/token"): JWT_B64,
            (
                "PUT",
                f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/configurations",
            ): {
                "tunnel_id": TUNNEL_UUID,
                "version": 3,
                "config": {},
                "source": "cloudflare",
            },
            ("GET", f"/client/v4/zones/{ZONE}/dns_records"): [
                {
                    "id": "rec-stale",
                    "type": "CNAME",
                    "name": HOSTNAME,
                    "content": "wrong-target.example.com",
                    "proxied": False,
                }
            ],
        }
        return _make_urlopen(handlers)(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0)
        CloudflaredApp().apply(ctx, {"ingress": {"base_domain": "bruj0.net"}})

    dns_put = [
        c
        for c in captured
        if c[0] == "PUT" and "dns_records/rec-stale" in c[1]
    ]
    assert len(dns_put) == 1
    _m, _u, body = dns_put[0]
    assert body["name"] == HOSTNAME
    assert body["content"] == f"{TUNNEL_UUID}.cfargotunnel.com"
    assert body["proxied"] is True


# ----- failure paths -----


def test_apply_raises_on_remote_config_failure(tmp_path: Path) -> None:
    """Cloudflare rejects the ingress rule -> RuntimeError.
    The orchestrator must NOT silently swallow this.
    """
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)
    (tmp_path / "infra" / "secrets" / "cloudflared-api-token.json").write_text(
        json.dumps({"id": "tid", "value": "tv"})
    )

    def _failing(req: object, timeout: float = 30.0, context: object = None):
        url = getattr(req, "full_url", str(req))
        if url.split("?", 1)[0].endswith("/configurations"):
            raise urllib.error.HTTPError(
                url,
                400,
                "Bad Request",
                {},
                io.BytesIO(_failing_response("bad ingress shape", 400)),
            )
        # Other endpoints: minimal valid envelope.
        handlers = {
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): [
                {"id": TUNNEL_UUID, "name": TUNNEL_NAME}
            ],
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/token"): JWT_B64,
            ("GET", f"/client/v4/zones/{ZONE}/dns_records"): [],
        }
        return _make_urlopen(handlers)(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_failing), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0)
        with pytest.raises(RuntimeError) as ei:
            CloudflaredApp().apply(
                ctx, {"ingress": {"base_domain": "bruj0.net"}}
            )
        assert "configurations" in str(ei.value) or "bad ingress" in str(ei.value)
    # Helm install must NOT have happened.
    assert not ctx.helm.install_or_upgrade.called


def test_apply_warns_but_succeeds_when_vaultwarden_seed_fails(
    tmp_path: Path,
) -> None:
    """vaultwarden-seed-note.py returning non-zero must be
    logged as a warning, NOT propagated. The helm install
    still happens (the chart-managed Secret is owned by
    helm at apply-time).
    """
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        return _make_urlopen(_cf_handlers_for_fresh_tunnel())(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(
            returncode=1, stdout="", stderr="vaultwarden unreachable"
        )
        result = CloudflaredApp().apply(
            ctx, {"ingress": {"base_domain": "bruj0.net"}}
        )

    assert result.app_name == "cloudflared"
    assert ctx.helm.install_or_upgrade.called

    # The audit log must record a `cloudflared.vws_seed_failed`
    # warning — we grep the audit_log file the logger writes
    # to rather than reaching into the StructuredLogger
    # internals.
    audit = (tmp_path / "logs" / "test_cloudflared.audit.jsonl").read_text()
    assert "cloudflared.vws_seed_failed" in audit


def test_apply_warns_when_vaultwarden_subprocess_unavailable(
    tmp_path: Path,
) -> None:
    """If the vaultwarden-seed-note.py script is missing
    or times out, the apply still completes. This pins
    the contract for environments where VWS is not yet
    up.
    """
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        return _make_urlopen(_cf_handlers_for_fresh_tunnel())(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch(
             "subprocess.run",
             side_effect=FileNotFoundError("uv: command not found"),
         ):
        result = CloudflaredApp().apply(
            ctx, {"ingress": {"base_domain": "bruj0.net"}}
        )

    assert result.app_name == "cloudflared"
    assert ctx.helm.install_or_upgrade.called
    audit = (tmp_path / "logs" / "test_cloudflared.audit.jsonl").read_text()
    assert "cloudflared.vws_seed_unavailable" in audit


# ----- destroy() -----


def test_destroy_uninstalls_helm_and_drops_namespace(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    CloudflaredApp().destroy(ctx, {"ingress": {"base_domain": "bruj0.net"}})

    ctx.helm.uninstall.assert_called_once()
    helm_args = ctx.helm.uninstall.call_args
    assert helm_args.args[0] == HELM_RELEASE_NAME
    assert helm_args.args[1] == NAMESPACE
    ctx.kubectl.delete_namespace.assert_called_once_with(NAMESPACE)


# ----- JWT base64-decoding contract -----


def test_jwt_persisted_in_decoded_form(tmp_path: Path) -> None:
    """The orchestrator base64-decodes Cloudflare's JWT
    response before persisting. Pins the contract that
    downstream consumers (helm --set, VWS note body)
    see the plaintext JWT.
    """
    import unittest.mock as _um

    raw_jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJhY2NvdW50IjoiMmU5YzA5YjI3ZDJhMDg5YzUzMWIxMmFlMGYwZTZmZjMi"
        "fQ.sig"
    )
    b64 = base64.b64encode(raw_jwt.encode("utf-8")).decode("ascii")

    ctx = _build_ctx(tmp_path)

    def _capture(req: object, timeout: float = 30.0, context: object = None):
        handlers = {
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): [],
            ("POST", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel"): {
                "id": TUNNEL_UUID,
                "name": TUNNEL_NAME,
                "config_src": "cloudflare",
                "credentials_file": {
                    "AccountTag": ACCOUNT,
                    "TunnelID": TUNNEL_UUID,
                    "TunnelSecret": "x",
                },
            },
            ("GET", f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/token"): b64,
            (
                "PUT",
                f"/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_UUID}/configurations",
            ): {
                "tunnel_id": TUNNEL_UUID,
                "version": 1,
                "config": {},
                "source": "cloudflare",
            },
            ("GET", f"/client/v4/zones/{ZONE}/dns_records"): [],
        }
        return _make_urlopen(handlers)(req, timeout, context)

    with _um.patch("urllib.request.urlopen", side_effect=_capture), \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0)
        CloudflaredApp().apply(ctx, {"ingress": {"base_domain": "bruj0.net"}})

    record = json.loads(
        (tmp_path / "infra" / "secrets" / "cloudflared-tunnel.json").read_text()
    )
    # The persisted JWT must be the decoded plaintext.
    assert record["tunnel_token"] == raw_jwt
    # And the helm values file must carry the same plaintext.
    helm_values_files = ctx.helm.install_or_upgrade.call_args.kwargs[
        "values_files"
    ]
    rendered = helm_values_files[0]
    rendered_text = Path(rendered).read_text()
    assert f"tunnel_token: '{raw_jwt}'" in rendered_text


# ----- hostname vs zone contract -----


def test_apply_raises_when_hostname_does_not_match_zone(tmp_path: Path) -> None:
    """If catalog.ingress.base_domain doesn't match the
    configured Cloudflare zone, apply must abort BEFORE
    touching Cloudflare or helm.
    """
    import unittest.mock as _um

    ctx = _build_ctx(tmp_path)
    with _um.patch("urllib.request.urlopen") as mock_urlopen, \
         _um.patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = MagicMock(returncode=0)
        with pytest.raises(RuntimeError) as ei:
            CloudflaredApp().apply(
                ctx, {"ingress": {"base_domain": "NOT-MATCHING.example"}}
            )
        assert "does not match" in str(ei.value)
    assert not mock_urlopen.called
    assert not ctx.helm.install_or_upgrade.called
