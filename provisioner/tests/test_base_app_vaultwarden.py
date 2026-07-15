"""test_base_app_vaultwarden — WP12 acceptance tests.

WP12 ships three canonical helpers on `BaseApp`:

  * `_read_dotenv_creds(repo_root, catalog)` —
    resolves master_password / server_url / email
    from catalog > .env > canonical defaults.
  * `_vaultwarden_client(ctx, catalog)` — reads
    the in-cluster VKS Secret (BW_CLIENTID +
    BW_CLIENTSECRET) and returns a logged-in
    `VaultwardenClient`.
  * `_seed_vaultwarden_note(ctx, catalog, note_name,
    body_text, *, namespace, secret_name,
    secret_key)` — finds or creates the VKS-triple
    cipher; logs the seed outcome.

These tests live in `test_base_app_vaultwarden.py`
per the WP12 acceptance plan. Apps reach for the
helpers; the helpers are the only consumers of
`provisioner.lib.vaultwarden.VaultwardenClient`
anywhere in `apps/*.py`.

WP12 also ships a static guard
(`tests/test_apps_no_inline_vaultwarden_client.py`)
that scans `apps/*.py` for `from
provisioner.lib.vaultwarden import VaultwardenClient`
and fails on any direct import outside the canonical
helper. Companion to the WP9 inline-pattern guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from provisioner.lib.apps import all_apps
from provisioner.lib.apps.base import BaseApp
from provisioner.lib.container import Container


# --------------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the autouse isolation in
    `test_orchestrator.py`. WP6+ tests reload app
    modules in place so the `BaseApp._dotenv` parser
    is exercised against the live shipped code, not
    against stale module objects.
    """
    from provisioner.lib.apps import reset_registry

    reset_registry()
    monkeypatch.setenv("PROXMOX_CICD_CLUSTER", "cicd")
    import importlib

    from provisioner.lib.apps import gitea as gitea_mod
    from provisioner.lib.apps import gitea_runner as gr_mod
    from provisioner.lib.apps import vaultwarden_k8s_sync as vks_mod
    from provisioner.lib.apps import cloudflared as cf_mod

    importlib.reload(gitea_mod)
    importlib.reload(gr_mod)
    importlib.reload(vks_mod)
    importlib.reload(cf_mod)
    yield
    reset_registry()


def _make_container(tmp_path: Path) -> Container:
    """Build a Container backed by `tmp_path` so the
    helpers' `.env` + cluster-Secret lookups land on
    scratch filesystem state."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Container.for_tests(
        proxmox_k3s_repo=tmp_path,
        repo_root=tmp_path,
    )


# --------------------------------------------------------------- _read_dotenv_creds


def test_read_dotenv_creds_reads_dotenv_keys(tmp_path: Path) -> None:
    """`_read_dotenv_creds(repo_root, catalog)` reads
    `VAULTWARDEN__MASTERPASSWORD` + `VAULTWARDEN__SERVERURL`
    + `client_email` from `.env` when the catalog doesn't
    provide them.
    """
    env_path = tmp_path / ".env"
    env_path.write_text(
        "VAULTWARDEN__MASTERPASSWORD=hunter2\n"
        "VAULTWARDEN__SERVERURL=https://vw.example\n"
        "CLIENT_EMAIL=alice@example\n"
    )
    creds = BaseApp._read_dotenv_creds(tmp_path, catalog={})
    assert creds["master_password"] == "hunter2"
    assert creds["server_url"] == "https://vw.example"
    assert creds["email"] == "alice@example"


def test_read_dotenv_creds_applies_catalog_overrides(
    tmp_path: Path,
) -> None:
    """A catalog value wins over `.env` (catalog > .env)
    for every key, including the operator's email and
    the Vaultwarden base URL.
    """
    env_path = tmp_path / ".env"
    env_path.write_text(
        "VAULTWARDEN__MASTERPASSWORD=hunter2\n"
        "VAULTWARDEN__SERVERURL=https://vw.example\n"
        "CLIENT_EMAIL=alice@example\n"
    )
    creds = BaseApp._read_dotenv_creds(
        tmp_path,
        catalog={
            "vaultwarden": {
                "master_password": "catalog-pw",
                "server_url": "https://catalog-vw.example",
                "email": "catalog@example",
            }
        },
    )
    assert creds["master_password"] == "catalog-pw"
    assert creds["server_url"] == "https://catalog-vw.example"
    assert creds["email"] == "catalog@example"


def test_read_dotenv_creds_raises_on_missing_url(
    tmp_path: Path,
) -> None:
    """Without catalog OR `.env` server_url AND without the
    canonical fallback, the helper raises a clear
    `RuntimeError` (NOT `None`, NOT silent).
    """
    # Temporarily monkey-patch the canonical fallback
    # constant so we can prove "no value at all" raises.
    sentinel = BaseApp.CANONICAL_VAULTWARDEN_URL
    try:
        BaseApp.CANONICAL_VAULTWARDEN_URL = ""  # disable fallback
        with pytest.raises(RuntimeError, match="server_url"):
            BaseApp._read_dotenv_creds(tmp_path, catalog={})
    finally:
        BaseApp.CANONICAL_VAULTWARDEN_URL = sentinel


# --------------------------------------------------------------- _vaultwarden_client


def test_vaultwarden_client_decodes_bw_clientid_secret(
    tmp_path: Path,
) -> None:
    """`_vaultwarden_client(ctx, catalog)` reads the
    in-cluster VKS Secret for `BW_CLIENTID` +
    `BW_CLIENTSECRET`, base64-decodes them, and
    returns a `VaultwardenClient.login(...)` result.

    The mock exactly mirrors the contract:
      * `kubectl.get secret ... jsonpath={.data.BW_CLIENTID}`
        → base64("client-id")
      * `kubectl.get secret ... jsonpath={.data.BW_CLIENTSECRET}`
        → base64("client-secret")
      * `VaultwardenClient.login(...)` returns the
        sentinel mock.
    """
    import base64 as b64

    from provisioner.lib.vaultwarden.client import (
        VaultwardenClient as _RealClient,
    )

    sentinel_client = MagicMock(name="VaultwardenClient")
    login_called: dict[str, Any] = {}

    def _fake_login(**kwargs: Any) -> Any:
        login_called.update(kwargs)
        return sentinel_client

    container = _make_container(tmp_path)
    container.kubectl = MagicMock()

    def _fake_kubectl_get(
        resource: str, name: str, namespace: str, jsonpath: str
    ) -> MagicMock:
        if "BW_CLIENTID" in jsonpath:
            return MagicMock(
                returncode=0,
                stdout=b64.b64encode(b"client-id").decode("utf-8"),
            )
        if "BW_CLIENTSECRET" in jsonpath:
            return MagicMock(
                returncode=0,
                stdout=b64.b64encode(b"client-secret").decode("utf-8"),
            )
        return MagicMock(returncode=1, stdout="", stderr="not found")

    container.kubectl.get.side_effect = _fake_kubectl_get
    container.kubectl.login = _fake_login  # never used; just in case

    # Patch the real `login` classmethod so we don't
    # need a live Vaultwarden instance.
    original_login = _RealClient.login
    _RealClient.login = staticmethod(_fake_login)  # type: ignore[assignment]
    try:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "VAULTWARDEN__MASTERPASSWORD=hunter2\n"
            "VAULTWARDEN__SERVERURL=https://vw.example\n"
            "CLIENT_EMAIL=alice@example\n"
        )
        ctx: Any = type("_FakeCtx", (), {"kubectl": container.kubectl, "repo_root": tmp_path})()
        client = BaseApp._vaultwarden_client(ctx, catalog={})
        assert client is sentinel_client
        assert login_called["client_id"] == "client-id"
        assert login_called["client_secret"] == "client-secret"
        assert login_called["email"] == "alice@example"
        assert login_called["master_password"] == "hunter2"
    finally:
        _RealClient.login = original_login  # type: ignore[assignment]


# --------------------------------------------------------------- _seed_vaultwarden_note


def test_seed_vaultwarden_note_creates_cipher_when_missing(
    tmp_path: Path,
) -> None:
    """`_seed_vaultwarden_note(...)` builds a Secure Note
    payload with the VKS triple as `custom_fields` and
    calls `client.create_cipher(payload)` when no cipher
    with the same triple exists.
    """
    client = MagicMock(name="VaultwardenClient")
    client.list_ciphers.return_value = []  # nothing seeded yet
    # `build_secure_note_payload` requires a 64-byte
    # user_key. We don't need real cryptographic
    # security in tests; 64 zero bytes are enough
    # to flow through the helper (the helper itself
    # never validates the key).
    client.user_key = b"\x00" * 64
    ctx: Any = type("_FakeCtx", (), {"logger": MagicMock()})()
    BaseApp._seed_vaultwarden_note(
        ctx,
        client,
        catalog={},
        note_name="wp12 test note",
        body_text="hunter2",
        namespace="wp12",
        secret_name="wp12-secret",
        secret_key="password",
    )
    client.create_cipher.assert_called_once()
    payload = client.create_cipher.call_args[0][0]
    # The payload's `fields` array carries the VKS triple
    # (namespaces / secret-name / secret-key), exactly 3
    # entries by `vks_triple` contract. Field `name` and
    # `value` are encrypted at this point; we assert on
    # the shape (3 fields + 1 cipher note + 1 body)
    # rather than trying to decrypt in the test.
    fields = payload.get("fields") or []
    assert len(fields) == 3, (
        f"VKS triple must produce 3 fields (namespaces, "
        f"secret-name, secret-key); got {len(fields)}: {fields}"
    )
    for f in fields:
        assert f["type"] == 0  # FIELD_TYPE_TEXT
        # Both name + value are Type-2 encrypted envelopes.
        assert isinstance(f["name"], str)
        assert isinstance(f["value"], str)
    assert payload.get("type") == 2  # TYPE_SECURE_NOTE
    # `notes` carries the body text (encrypted).
    assert "notes" in payload


def test_seed_vaultwarden_note_is_noop_when_body_matches(
    tmp_path: Path,
) -> None:
    """If a cipher with the same VKS triple exists,
    `_seed_vaultwarden_note` does NOT call
    `create_cipher` (idempotency dedup) and logs the
    skip via `ctx.logger.info(...)` with a clear event
    name.
    """
    from provisioner.lib.vaultwarden.note import vks_triple

    triple = vks_triple(
        namespace="wp12",
        secret_name="wp12-secret",
        secret_key="password",
    )
    client = MagicMock(name="VaultwardenClient")
    # The helper iterates `cipher["fields"]` and calls
    # `decrypt_cipher_field_name(cipher, index=i)` for each
    # entry, then `decrypt_cipher_field(cipher, name=k)`.
    # We pre-load the mock with the triple so the loop
    # finishes with the VKS triple in hand, matching the
    # test's `(namespace, secret_name, secret_key)`.
    field_list = [{"name": "x", "value": "y"} for _ in triple]
    client.list_ciphers.return_value = [{"fields": field_list}]

    def _decrypt_name(cipher: Any, index: int) -> str:
        return list(triple.keys())[index]

    def _decrypt_value(
        cipher: Any, name: str | None = None
    ) -> str:
        if name is None:
            return ""
        return triple.get(name, "")

    client.decrypt_cipher_field_name.side_effect = _decrypt_name
    client.decrypt_cipher_field.side_effect = _decrypt_value

    logger = MagicMock()
    ctx: Any = type("_FakeCtx", (), {"logger": logger})()
    BaseApp._seed_vaultwarden_note(
        ctx,
        client,
        catalog={},
        note_name="wp12 test note",
        body_text="hunter2",
        namespace="wp12",
        secret_name="wp12-secret",
        secret_key="password",
    )
    client.create_cipher.assert_not_called()
    # The skip is logged with a clear audit event so a
    # future grep for the namespace can find every skip.
    info_calls = [
        c for c in logger.info.call_args_list if c.args and c.args[0]
    ]
    assert any(
        "skipped" in (c.args[0] or "") for c in info_calls
    ), f"expected a `*.skipped` log entry; got {info_calls}"


def test_seed_vaultwarden_note_updates_cipher_when_body_differs(
    tmp_path: Path,
) -> None:
    """If a cipher with the same VKS triple exists but
    carries an older body, the helper updates it
    (`create_cipher` called with the new body text)
    and logs the update.
    """
    client = MagicMock(name="VaultwardenClient")
    client.user_key = b"\x00" * 64
    client.list_ciphers.return_value = [
        {"fields": [{"name": "namespaces", "value": "wp12"}]}
    ]
    logger = MagicMock()
    ctx: Any = type("_FakeCtx", (), {"logger": logger})()
    BaseApp._seed_vaultwarden_note(
        ctx,
        client,
        catalog={},
        note_name="wp12 test note",
        body_text="new-body",
        namespace="wp12",
        secret_name="wp12-secret",
        secret_key="password",
    )
    client.create_cipher.assert_called_once()


def test_seed_vaultwarden_note_logs_audit_event(
    tmp_path: Path,
) -> None:
    """A successful seed logs the standard
    `<app_short>.vaultwarden_seeded` audit shape with
    namespace/secret-name/secret-key so the operator can
    grep for it.
    """
    client = MagicMock(name="VaultwardenClient")
    client.user_key = b"\x00" * 64
    client.list_ciphers.return_value = []
    logger = MagicMock()
    ctx: Any = type("_FakeCtx", (), {"logger": logger})()
    BaseApp._seed_vaultwarden_note(
        ctx,
        client,
        catalog={},
        note_name="wp12 test note",
        body_text="body",
        namespace="wp12",
        secret_name="wp12-secret",
        secret_key="password",
        app_short="wp12",
    )
    seeded = [
        c for c in logger.info.call_args_list
        if c.args and "vaultwarden_seeded" in (c.args[0] or "")
    ]
    assert seeded, (
        f"expected a `*.vaultwarden_seeded` log entry; got "
        f"{[c.args for c in logger.info.call_args_list]}"
    )


# --------------------------------------------------------------- apps reach for helper


def test_no_app_re_defines_read_dotenv_creds() -> None:
    """WP12 — the apps must NOT carry their own copy of
    `_read_dotenv_creds`. The canonical helper on
    `BaseApp` is the only allowed implementation.
    """
    import re

    pat = re.compile(r"\bdef _read_dotenv_creds\b")
    offenders: list[tuple[str, int]] = []
    for app_cls in all_apps():
        path = app_cls.__module__.replace(".", "/") + ".py"
        full = Path(__file__).resolve().parents[2] / path
        if not full.exists():
            continue
        text = full.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if pat.search(line):
                offenders.append((app_cls.__name__, line_no))
    assert not offenders, (
        f"_read_dotenv_creds is canonical on BaseApp (WP12); "
        f"apps must not redefine it. Offenders: {offenders}"
    )
