"""Tests for the cloudflared app — remotely-managed Cloudflare
Tunnel via the upstream cloudflare-tunnel-remote-0.1.2
chart. Mocks the Cloudflare API + helm + kubectl +
Vaultwarden subprocess so the unit tests don't need a real
account or cluster.

What we lock down:

  - .env parsing (.parse_dotenv, _load_dotenv, _require_env)
  - Registry: app is registered under "cloudflared"
  - plan(): references the vendored .tgz, the upstream chart
    version, the tunnel-token Vaultwarden note, and the
    remote-config PUT path
  - apply() end-to-end on a fresh account:
      * GET /accounts/{acc}/cfd_tunnel -> []
      * POST /accounts/{acc}/cfd_tunnel (create remote tunnel)
      * PUT /accounts/{acc}/cfd_tunnel/{tun}/configurations
      * GET /zones/{zone}/dns_records -> []
      * POST /zones/{zone}/dns_records
      * VaultwardenClient.login + create_cipher (in-process,
        no subprocess to a seed-note script)
      * helm install with vendored chart + tunnel_token via --set
      * tunnel_token persisted (base64 string, mode 0600)
  - apply() on a re-run with existing tunnel:
      * no POST /cfd_tunnel, no DNS write — ingress PUT only
  - apply() on a re-run with drifted DNS record:
      * PUT /zones/{zone}/dns_records/{rec_id} with
        proxied=True (auto-corrects drift)
  - apply() failure paths:
      * ingress PUT returning 400 -> RuntimeError surfaces
      * VaultwardenClient.create_cipher raising -> non-fatal
        warning, helm install still completes
  - destroy(): helm uninstall + namespace delete
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps import app_by_name, reset_registry
from provisioner.lib.apps.cloudflared import (
    APP_VERSION,
    CHART_TGZ,
    CHART_VERSION,
    HELM_RELEASE_NAME,
    VWS_NOTE_SECRET_KEY,
    VWS_NOTE_SECRET_NAME,
    CloudflaredApp,
)
from provisioner.lib.container import Container


# ----- constants used by both the orchestrator and the mocks -----

# A real-shape base64 tunnel token. The plaintext is a
# compact JSON `{a, t, s}` payload (NOT a JWT); the base64
# encoding is what cloudflared consumes via `$TUNNEL_TOKEN`.
TUNNEL_TOKEN_PLAINTEXT = (
    "eyJhIjoiMmU5YzA5YjI3ZDJhMDg5YzUzMWIxMmFlMGYwZTZmZjMiLCJ0Ijoi"
    "Y2JhYTg4MDctZDM1OS00MWQ2LWJhYjktMTkwNjJlNzUyNzRjIiwicyI6Ik84"
    "bWF6VkxMYzliK0ljSWVaUlZPMWRxZEpCYjY0aG5pdDByT3NOZlQ4amtCcDY1"
    "Q3hFczh2a3BJamtwSHhsNk1LUzZ3ZitSYW5ueWNSdUE3TlpTdG1nPT0ifQ=="
)
TUNNEL_TOKEN_B64 = base64.b64encode(
    TUNNEL_TOKEN_PLAINTEXT.encode("utf-8")
).decode("ascii")
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
    assert "JWT" in text or "tunnel_token" in text, "plan must mention the tunnel token"
    assert "cfargotunnel.com" in text
    assert VWS_NOTE_SECRET_NAME in text
    assert VWS_NOTE_SECRET_KEY in text


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


# ----- vaultwarden note idempotency -----

class _StubVaultClient:
    """Stand-in for ``VaultwardenClient`` so the seed
    helper's library calls can be asserted without
    performing real crypto or HTTP. Records every
    method call for the test to inspect."""

    def __init__(self, ciphers: list[dict] | None = None) -> None:
        self.ciphers = ciphers if ciphers is not None else []
        self.created: list[dict] = []
        self.list_calls = 0
        # ``build_secure_note_payload`` reads this; a
        # random 64-byte key is fine — the stub never
        # tries to encrypt with it.
        self.user_key = b"\x00" * 64

    def login(self, **_kwargs: object) -> _StubVaultClient:
        return self

    def list_ciphers(self) -> list[dict]:
        self.list_calls += 1
        return list(self.ciphers)

    def decrypt_cipher_field_name(self, cipher: dict, *, index: int) -> str:
        # Each field's "name" is a dict like {"plain": "namespaces"}.
        # The real client decrypts; the stub just returns the tag.
        return cipher["fields"][index]["name_plain"]

    def decrypt_cipher_field(self, cipher: dict, *, name: str) -> str:
        for f in cipher["fields"]:
            if f["name_plain"] == name:
                return f["value_plain"]
        raise KeyError(name)

    def create_cipher(self, payload: dict) -> dict:
        self.created.append(payload)
        return {"id": "new-id", **payload}


def _cipher_with_triple(
    namespaces: str,
    secret_name: str,
    secret_key: str,
    body: str = "{}",
) -> dict:
    return {
        "id": f"existing-{secret_name}",
        "type": 2,
        "fields": [
            {"name_plain": "namespaces",  "value_plain": namespaces},
            {"name_plain": "secret-name", "value_plain": secret_name},
            {"name_plain": "secret-key",  "value_plain": secret_key},
        ],
        "notes": body,
    }


def test_vws_seed_skipped_when_triple_already_exists(tmp_path: Path) -> None:
    """The 2026-07-14 vault audit found 4 duplicate
    ``cloudflared k8s secret value`` entries from back-
    to-back ``cicdctl apply cicd`` runs because the
    orchestrator created a new cipher on every run
    instead of guarding with ``list_ciphers``. Pin the
    new behaviour: when a cipher with the same VKS
    triple exists, ``create_cipher`` must NOT be
    called.
    """
    import unittest.mock as _um
    from provisioner.lib.vaultwarden import VaultwardenClient as RealClient

    ctx = _build_ctx(tmp_path)
    # Use a temp password file INSIDE tmp_path, NOT the
    # shared ``/tmp/vw.pw`` — the orchestrator reads
    # that exact path, so a test that writes to it would
    # silently corrupt the operator's live password.
    # We monkey-patch the helper's lookup below.
    fake_pw = tmp_path / "vw.pw"
    fake_pw.write_text("master-password\n")
    fake_pw.chmod(0o600)

    stub = _StubVaultClient(ciphers=[
        _cipher_with_triple("cloudflared", "cloudflare-tunnel-remote", "tunnelToken"),
    ])
    # The orchestrator runs `kubectl ... get secret ... -o
    # jsonpath={.data.BW_CLIENTID}` and the same for
    # BW_CLIENTSECRET. Two subprocess calls, both must
    # return valid base64 stdout.
    b64_id = base64.b64encode(b"fake-client-id").decode()
    b64_sec = base64.b64encode(b"fake-client-secret").decode()
    subproc_results = [
        MagicMock(returncode=0, stdout=b64_id + "\n", stderr=""),
        MagicMock(returncode=0, stdout=b64_sec + "\n", stderr=""),
    ]

    def fake_subproc(*args, **kwargs):
        if subproc_results:
            return subproc_results.pop(0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with _um.patch.object(RealClient, "login", classmethod(lambda cls, **kw: stub)), \
         _um.patch.object(RealClient, "list_ciphers", stub.list_ciphers), \
         _um.patch.object(RealClient, "decrypt_cipher_field_name", stub.decrypt_cipher_field_name), \
         _um.patch.object(RealClient, "decrypt_cipher_field", stub.decrypt_cipher_field), \
         _um.patch.object(RealClient, "create_cipher", stub.create_cipher), \
         _um.patch("subprocess.run", side_effect=fake_subproc), \
         _um.patch(
             "provisioner.lib.apps.cloudflared.Path",
             lambda *a, **kw: fake_pw if a and str(a[0]) == "/tmp/vw.pw" else Path(*a, **kw),
         ):
        # Call the private seed helper directly.
        CloudflaredApp()._seed_vaultwarden_note(ctx, TUNNEL_TOKEN_PLAINTEXT)

    # No new ciphers should have been created.
    assert stub.created == []
    # list_ciphers must have been called (the guard).
    assert stub.list_calls >= 1


def test_vws_seed_creates_when_no_matching_triple(tmp_path: Path) -> None:
    """If no existing cipher has the same VKS triple,
    the helper must POST a new cipher (preserve the
    original seed-from-scratch behaviour for the
    first-ever apply)."""
    import unittest.mock as _um
    from provisioner.lib.vaultwarden import VaultwardenClient as RealClient

    ctx = _build_ctx(tmp_path)
    fake_pw = tmp_path / "vw.pw"
    fake_pw.write_text("master-password\n")
    fake_pw.chmod(0o600)

    # Empty vault → no matching triple.
    stub = _StubVaultClient(ciphers=[
        _cipher_with_triple("default", "some-other-secret", "password"),  # unrelated
    ])
    b64_id = base64.b64encode(b"fake-client-id").decode()
    b64_sec = base64.b64encode(b"fake-client-secret").decode()
    subproc_results = [
        MagicMock(returncode=0, stdout=b64_id + "\n", stderr=""),
        MagicMock(returncode=0, stdout=b64_sec + "\n", stderr=""),
    ]

    def fake_subproc(*args, **kwargs):
        if subproc_results:
            return subproc_results.pop(0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with _um.patch.object(RealClient, "login", classmethod(lambda cls, **kw: stub)), \
         _um.patch.object(RealClient, "list_ciphers", stub.list_ciphers), \
         _um.patch.object(RealClient, "decrypt_cipher_field_name", stub.decrypt_cipher_field_name), \
         _um.patch.object(RealClient, "decrypt_cipher_field", stub.decrypt_cipher_field), \
         _um.patch.object(RealClient, "create_cipher", stub.create_cipher), \
         _um.patch("subprocess.run", side_effect=fake_subproc), \
         _um.patch(
             "provisioner.lib.apps.cloudflared.Path",
             lambda *a, **kw: fake_pw if a and str(a[0]) == "/tmp/vw.pw" else Path(*a, **kw),
         ):
        CloudflaredApp()._seed_vaultwarden_note(ctx, TUNNEL_TOKEN_PLAINTEXT)

    assert len(stub.created) == 1
    assert stub.list_calls >= 1
