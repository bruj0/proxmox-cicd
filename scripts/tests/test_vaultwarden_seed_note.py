"""Tests for scripts/vaultwarden-seed-note.py

Crypto round-trip + dry-run path tests. We mock the
Vaultwarden HTTP surface (urllib.request.urlopen) so the
tests run without a real server. The goal is to lock down
the Bitwarden encryption shape + the request payload
contract.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the script importable as a module. We add scripts/ to
# sys.path and load the file via importlib because the filename
# has a hyphen, which is not a valid Python module name.
import importlib.util

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "vaultwarden_seed_note",
    SCRIPTS / "vaultwarden-seed-note.py",
)
vsn = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["vaultwarden_seed_note"] = vsn
_spec.loader.exec_module(vsn)  # type: ignore[union-attr]


# ---------- crypto primitives ----------

def test_make_master_key_is_deterministic() -> None:
    a = vsn.make_master_key("hunter2", "ops@example.com", 100_000)
    b = vsn.make_master_key("hunter2", "ops@example.com", 100_000)
    c = vsn.make_master_key("hunter3", "ops@example.com", 100_000)
    assert a == b
    assert a != c
    assert len(a) == 32


def test_make_server_auth_hash_matches_bitwarden_test_vector() -> None:
    """Pinned against the official Bitwarden Rust test vector
    in ``bitwarden_crypto::keys::master_key::tests::
    test_password_hash_pbkdf2``: password="asdfasdf",
    salt="test@bitwarden.com", iterations=100_000 →
    server hash "wmyadRMyBZOH7P/a/ucTCbSghKgdzDpPqUnu/DAVtSw=".
    If this test ever drifts from that string, the script
    will silently fail to authenticate against any
    Vaultwarden instance with a 400 "Username or password is
    incorrect" — the script used to do exactly that until
    we pinned the test against the reference impl.

    The trailing '=' MUST be kept. Vaultwarden's
    verify_password_hash uses the literal string bytes
    (including padding) as the PBKDF2 input — see
    ``make_server_auth_hash`` docstring.
    """
    master_key = vsn.make_master_key(
        "asdfasdf", "test@bitwarden.com", 100_000
    )
    auth = vsn.make_server_auth_hash(master_key, "asdfasdf")
    # The Rust reference impl asserts the WITH-padding form.
    # This is exactly what the official bw CLI sends.
    assert auth == "wmyadRMyBZOH7P/a/ucTCbSghKgdzDpPqUnu/DAVtSw="
    assert auth.endswith("=")


def test_make_server_auth_hash_preserves_padding() -> None:
    """make_server_auth_hash MUST return the base64 with the
    trailing '=' padding preserved. Stripping the padding
    was a real bug found on 2026-07-14: the script sent
    ``...+XjQ`` (43 chars), the official ``bw`` CLI sent
    ``...+XjQ=`` (44 chars); Vaultwarden's PBKDF2-verify
    treats the two strings as different inputs and only the
    padded form matched the stored hash. Verify the
    returned string round-trips through stdlib base64
    decode WITHOUT requiring padding to be re-added.
    """
    master_key = b"\xab" * 32
    auth = vsn.make_server_auth_hash(master_key, "correctpw")
    # The returned string should be a fully-padded base64
    # encoding — must decode cleanly with stdlib's strict
    # base64.b64decode (which rejects unpadded input).
    decoded = base64.b64decode(auth, validate=True)
    assert len(decoded) == 32
    # And it must equal the direct PBKDF2 computation
    # (with the password as the salt, NOT the stretched
    # key — that was an older bug).
    digest = __import__("hashlib").pbkdf2_hmac(
        "sha256", master_key, b"correctpw", 1, dklen=32
    )
    assert base64.b64encode(digest).decode("ascii") == auth


def test_aes_cbc_encrypt_format() -> None:
    """The (iv, ct, mac) triple from aes_cbc_encrypt must be
    bit-width compatible with what unwrap_user_key expects.
    """
    enc_key, mac_key = b"e" * 32, b"m" * 32
    iv, ct, mac_digest = vsn.aes_cbc_encrypt(enc_key, mac_key, b"hello world")
    assert len(iv) == 16
    assert len(ct) % 16 == 0  # AES-CBC pads to 16
    assert len(mac_digest) == 32  # SHA-256


def test_stretch_master_key_returns_distinct_32byte_keys() -> None:
    """HKDF-Expand with the Bitwarden "enc"/"mac" labels must
    produce two distinct 32-byte keys derived from the same
    32-byte master key. Pinning this here means a future
    refactor can't accidentally merge them (which would make
    unwrap_user_key silently accept any wrapped blob — a
    serious correctness bug).
    """
    master_key = b"\x42" * 32
    enc_key, mac_key = vsn.stretch_master_key(master_key)
    assert len(enc_key) == 32
    assert len(mac_key) == 32
    assert enc_key != mac_key
    assert enc_key != master_key
    assert mac_key != master_key


def test_unwrap_user_key_round_trip() -> None:
    """Encrypt a 64-byte user key with AES-CBC, wrap with the
    Bitwarden envelope (using the HKDF-stretched enc/mac
    keys), then unwrap. The unwrapped key must equal the
    original.
    """

    user_key = bytes(range(64))  # arbitrary 64-byte key
    master_key = b"\x42" * 32
    enc_key, mac_key = vsn.stretch_master_key(master_key)
    iv, ct, mac_digest = vsn.aes_cbc_encrypt(enc_key, mac_key, user_key)
    envelope = f"{vsn.ENC_TYPE}.{vsn.b64(iv)}|{vsn.b64(ct)}|{vsn.b64(mac_digest)}"
    out = vsn.unwrap_user_key(master_key, envelope)
    assert out == user_key


def test_unwrap_user_key_rejects_bad_mac() -> None:
    user_key = b"\x10" * 64
    master_key = b"\x42" * 32
    enc_key, mac_key = vsn.stretch_master_key(master_key)
    iv, ct, mac_digest = vsn.aes_cbc_encrypt(enc_key, mac_key, user_key)
    # Flip a byte in the MAC — unwrap must refuse.
    bad_mac = bytearray(mac_digest)
    bad_mac[0] ^= 0xFF
    envelope = (
        f"{vsn.ENC_TYPE}.{vsn.b64(iv)}|{vsn.b64(ct)}|{vsn.b64(bytes(bad_mac))}"
    )
    try:
        vsn.unwrap_user_key(master_key, envelope)
    except SystemExit as exc:
        assert "MAC" in str(exc)
    else:
        raise AssertionError("unwrap must refuse bad MAC")


def test_encrypt_str_for_vault_round_trip() -> None:
    """encrypt_str_for_vault produces the Bitwarden envelope
    shape; an unwrap-with-the-same-key round trip recovers
    the original string.
    """
    # Round-trip via aes_cbc_decrypt; we don't have a decrypt
    # helper in the script, so build one inline.
    user_key = bytes(range(64))
    enc_key, mac_key = vsn.split_user_key(user_key)
    envelope = vsn.encrypt_str_for_vault("hello, vault", user_key)
    # Envelope shape: "2.<b64iv>|<b64ct>|<b64mac>" — IV first.
    parts = envelope.split(".")
    assert len(parts) == 2
    enc_type, blob = parts
    assert int(enc_type) == vsn.ENC_TYPE
    iv_b64, ct_b64, mac_b64 = blob.split("|")
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    mac_digest = base64.b64decode(mac_b64)
    # MAC verify — must use mac_key, NOT enc_key. (Bug fixed
    # on 2026-07-14: encrypt_str_for_vault was using enc_key
    # for both AES and HMAC, which only round-tripped because
    # the test helper was matching that wrong shape.)
    import hashlib
    import hmac as _hmac

    expected = _hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    assert _hmac.compare_digest(expected, mac_digest)
    # CBC decrypt + strip PKCS7
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    plaintext = padded[:-pad]
    assert plaintext.decode("utf-8") == "hello, vault"


# ---------- orchestration ----------

def test_load_body_literal() -> None:
    assert vsn.load_body("literal-value") == "literal-value"


def test_load_body_at_file(tmp_path: Path) -> None:
    f = tmp_path / "creds.json"
    f.write_text('{"a": 1}')
    assert vsn.load_body(f"@{f}") == '{"a": 1}'


def test_load_body_stdin() -> None:
    fake_stdin = io.StringIO("stdin-value\n")
    with patch.object(sys, "stdin", fake_stdin):
        assert vsn.load_body("-") == "stdin-value"


def test_load_body_stdin_preserves_internal_whitespace() -> None:
    fake_stdin = io.StringIO("  leading-and-trailing  \n")
    with patch.object(sys, "stdin", fake_stdin):
        # We strip the lone trailing newline echo adds, but
        # internal whitespace is preserved verbatim.
        assert vsn.load_body("-") == "  leading-and-trailing  "


def test_build_secure_note_payload_encrypts_everything() -> None:
    """The note body, the display name, and every custom
    field value must be encrypted before being sent to the
    API. We can't decrypt here without a decrypt helper, but
    we can assert the envelope shape on every string.
    """
    user_key = bytes(range(64))
    payload = vsn.build_secure_note_payload(
        note_name="my app note",
        body_text="the secret value",
        custom_fields={
            "namespaces": "myapp",
            "secret-name": "myapp-config",
            "secret-key": "password",
        },
        user_key=user_key,
    )
    assert payload["type"] == vsn.TYPE_SECURE_NOTE
    assert payload["secureNote"] == {"type": vsn.SECURE_NOTE_GENERIC}
    assert payload["favorite"] is False
    # Every string must be in the "2.<ct>|<iv>|<mac>" shape.
    for s in (payload["name"], payload["notes"]):
        assert s.startswith(f"{vsn.ENC_TYPE}."), s
        assert s.count("|") == 2
    assert len(payload["fields"]) == 3
    for f in payload["fields"]:
        assert f["type"] == vsn.FIELD_TYPE_TEXT
        assert f["name"].startswith(f"{vsn.ENC_TYPE}.")
        assert f["value"].startswith(f"{vsn.ENC_TYPE}.")


# ---------- main() end-to-end (mocked HTTP) ----------

@contextmanager
def _mocked_http(*, prelogin_resp: dict, login_resp: dict, profile_resp: dict):
    """Patch urllib.request.urlopen + the BW_SECRET Secret read
    so main() can run end-to-end without touching the network.
    """
    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    def fake_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        # prelogin POST
        if "/identity/accounts/prelogin" in url:
            return _FakeResp(json.dumps(prelogin_resp).encode("utf-8"))
        # token POST
        if "/identity/connect/token" in url:
            return _FakeResp(json.dumps(login_resp).encode("utf-8"))
        # profile GET
        if "/api/accounts/profile" in url:
            return _FakeResp(json.dumps(profile_resp).encode("utf-8"))
        # ciphers POST — capture body
        if "/api/ciphers" in url:
            body_bytes = req.data if isinstance(req.data, bytes) else b""
            captured["cipher_request"] = json.loads(body_bytes.decode("utf-8"))
            return _FakeResp(json.dumps({"id": "cipher-uuid-1"}).encode("utf-8"))
        raise AssertionError(f"unexpected URL in test: {url}")

    captured: dict[str, object] = {}
    with (
        patch.object(vsn, "_OPENER"),
        patch.object(vsn, "read_vks_secret", return_value=("user.abc", "secret")),
        patch.dict(
            vsn.os.environ,
            {"VAULTWARDEN__EMAIL": "ops@example.com"},
            clear=False,
        ),
        patch("getpass.getpass", return_value="correct horse battery staple"),
    ):
        vsn._OPENER.open = MagicMock(side_effect=fake_urlopen)
        yield captured


def test_main_creates_secure_note_with_correct_vks_fields() -> None:
    """main() must log in, fetch the user key, encrypt the
    body + the VKS custom fields, and POST /api/ciphers with
    a Secure Note whose fields encode (namespaces, secret-
    name, secret-key) — the same triple every VKS app uses.
    """
    user_key = bytes(range(64))

    # Pre-compute what the server's encrypted user key would
    # look like if it wrapped `user_key` with master_key
    # (using the HKDF-stretched enc/mac keys, per Bitwarden
    # Type-2 envelope convention).
    master_key = vsn.make_master_key(
        "correct horse battery staple", "ops@example.com", 100_000
    )
    enc_key, mac_key = vsn.stretch_master_key(master_key)
    iv, ct, mac_digest = vsn.aes_cbc_encrypt(enc_key, mac_key, user_key)
    profile_key = f"{vsn.ENC_TYPE}.{vsn.b64(iv)}|{vsn.b64(ct)}|{vsn.b64(mac_digest)}"

    with _mocked_http(
        prelogin_resp={"kdfIterations": 100_000, "kdfType": 0},
        login_resp={"access_token": "tok-1", "expires_in": 3600},
        profile_resp={"key": profile_key, "email": "ops@example.com"},
    ) as captured:
        rc = vsn.main(
            [
                "--app", "cloudflared",
                "--namespace", "cloudflared",
                "--secret-name", "cloudflared-cloudflare-tunnel",
                "--secret-key", "credentials.json",
                "--body", '{"a":"acct","t":"tunnel-uuid","s":"secret"}',
            ]
        )
    assert rc == 0
    assert "cipher_request" in captured
    req = captured["cipher_request"]
    assert req["type"] == vsn.TYPE_SECURE_NOTE
    assert req["secureNote"] == {"type": vsn.SECURE_NOTE_GENERIC}
    # Three custom fields, each encrypted with the user key.
    assert len(req["fields"]) == 3
    field_names = sorted(
        # Decrypt the field NAME to compare
        [_decrypt_field_name(user_key, f["name"]) for f in req["fields"]]
    )
    assert field_names == ["namespaces", "secret-key", "secret-name"]


def test_main_dry_run_skips_http_post() -> None:
    user_key = bytes(range(64))
    master_key = vsn.make_master_key(
        "correct horse battery staple", "ops@example.com", 100_000
    )
    enc_key, mac_key = vsn.stretch_master_key(master_key)
    iv, ct, mac_digest = vsn.aes_cbc_encrypt(enc_key, mac_key, user_key)
    profile_key = f"{vsn.ENC_TYPE}.{vsn.b64(iv)}|{vsn.b64(ct)}|{vsn.b64(mac_digest)}"

    post_calls = {"n": 0}

    def fake_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/identity/accounts/prelogin" in url:
            return MagicMock(read=lambda: json.dumps({"kdfIterations": 100_000}).encode("utf-8"), __enter__=lambda s: s, __exit__=lambda *a: None)
        if "/identity/connect/token" in url:
            return MagicMock(read=lambda: json.dumps({"access_token": "tok-1"}).encode("utf-8"), __enter__=lambda s: s, __exit__=lambda *a: None)
        if "/api/accounts/profile" in url:
            return MagicMock(read=lambda: json.dumps({"key": profile_key}).encode("utf-8"), __enter__=lambda s: s, __exit__=lambda *a: None)
        if "/api/ciphers" in url:
            post_calls["n"] += 1
            return MagicMock(read=lambda: b"{}", __enter__=lambda s: s, __exit__=lambda *a: None)
        raise AssertionError(f"unexpected URL: {url}")

    with (
        patch.object(vsn, "_OPENER"),
        patch.object(vsn, "read_vks_secret", return_value=("user.abc", "secret")),
        patch.dict(vsn.os.environ, {"VAULTWARDEN__EMAIL": "ops@example.com"}, clear=False),
        patch("getpass.getpass", return_value="correct horse battery staple"),
    ):
        vsn._OPENER.open = MagicMock(side_effect=fake_urlopen)
        rc = vsn.main(
            [
                "--app", "cloudflared",
                "--namespace", "cloudflared",
                "--secret-name", "cloudflared-cloudflare-tunnel",
                "--secret-key", "credentials.json",
                "--body", "x",
                "--dry-run",
            ]
        )
    assert rc == 0
    assert post_calls["n"] == 0, "dry-run must not POST /api/ciphers"


# ---------- helpers ----------

def _decrypt_field_name(user_key: bytes, envelope: str) -> str:
    """Decrypt a single encrypted string with the user key's
    enc + mac subkeys. Used by tests to assert the right
    custom-field NAMES were sent.
    """
    enc_key, mac_key = vsn.split_user_key(user_key)
    enc_type, blob = envelope.split(".")
    assert int(enc_type) == vsn.ENC_TYPE
    iv_b64, ct_b64, mac_b64 = blob.split("|")
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    mac_digest = base64.b64decode(mac_b64)
    import hashlib
    import hmac as _hmac
    expected = _hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    assert _hmac.compare_digest(expected, mac_digest)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    return padded[:-pad].decode("utf-8")


# ---------- email resolution ----------

def test_derive_email_defaults_to_secrets_account() -> None:
    """When neither --email nor $VAULTWARDEN__EMAIL is set,
    the script must default to the dedicated secrets-only
    Vaultwarden account (secrets@bruj0.net) — NOT the
    Cloudflare admin email or any other address. VKS is
    wired to that account; using the wrong one produces
    a successful login to a different vault, which is the
    worst possible failure mode (silent, no error).
    """
    with patch.dict(vsn.os.environ, {}, clear=True):
        assert vsn.derive_email() == "secrets@bruj0.net"


def test_derive_email_env_override() -> None:
    with patch.dict(
        vsn.os.environ, {"VAULTWARDEN__EMAIL": "ops@example.com"}, clear=True
    ):
        assert vsn.derive_email() == "ops@example.com"


def test_derive_email_cli_flag_wins() -> None:
    with patch.dict(
        vsn.os.environ, {"VAULTWARDEN__EMAIL": "ops@example.com"}, clear=True
    ):
        assert vsn.derive_email("cli@example.com") == "cli@example.com"


def test_derive_email_does_not_fall_back_to_cloudflare_email() -> None:
    """Regression: a previous version of this script fell
    back to $CLOUDFLARE_GLOBAL_API_EMAIL when no
    Vaultwarden email was set. That address is the
    Cloudflare admin, NOT a Vaultwarden user — using it
    silently targets the wrong account. We now require
    the dedicated secrets@bruj0.net by default.
    """
    with patch.dict(
        vsn.os.environ,
        {"CLOUDFLARE_GLOBAL_API_EMAIL": "ramakandra@gmail.com"},
        clear=True,
    ):
        # Still defaults to the dedicated secrets account —
        # the Cloudflare admin email must NOT be picked up.
        assert vsn.derive_email() == "secrets@bruj0.net"


def test_opener_default_headers_include_required_bitwarden_client_version() -> None:
    """Vaultwarden's auth.rs FromRequest impl rejects the
    /identity/connect/token endpoint with the error
    'No Bitwarden-Client-Version header provided' if the
    header is missing. The script's opener must install
    it as a default header so every call site picks it
    up automatically. If this test ever fails, expect a
    400 from /identity/connect/token with the message
    'Unauthorized Error: No Bitwarden-Client-Version
    header provided' in the Vaultwarden server log.
    """
    opener = vsn._build_opener()
    headers = dict(opener.addheaders)
    # Required by Vaultwarden: semver-shaped string.
    assert "Bitwarden-Client-Version" in headers, (
        f"missing Bitwarden-Client-Version header; got {list(headers)}"
    )
    assert headers["Bitwarden-Client-Version"], (
        "Bitwarden-Client-Version must be a non-empty string"
    )
    # Cloudflare WAF workaround.
    assert "User-Agent" in headers
    assert headers["User-Agent"] != f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"
    # device-type is optional but recommended; matches the
    # form field we send on /identity/connect/token.
    assert "device-type" in headers
    assert headers["device-type"] == "25"


def test_module_level_opener_has_bitwarden_client_version_header() -> None:
    """The module-level ``_OPENER`` is what every call site
    actually uses. We pin its headers here (not just the
    factory's output) so a future refactor that
    re-instantiates ``_OPENER`` can't drop the header.
    """
    headers = dict(vsn._OPENER.addheaders)
    assert "Bitwarden-Client-Version" in headers
    assert "User-Agent" in headers
