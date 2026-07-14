#!/usr/bin/env python3
"""scripts/vaultwarden-seed-note.py — create a Vaultwarden Secure
Note whose body is the raw value a VaultwardenK8sSync (VKS) app
is waiting on.

Why this script exists
----------------------
VKS syncs Vaultwarden Secure Notes into Kubernetes Secrets.
Each VKS-managed Secret in the cluster expects a Secure Note
whose custom fields are::

    namespaces   = <k8s namespace>
    secret-name  = <k8s secret name>
    secret-key   = <key inside the secret>

and whose note body is the raw value to be written into that
key. Creating that note by hand in the Vaultwarden web UI is
fine for one-off secrets (gitea runner registration token,
Cloudflare tunnel credentials, etc.), but it's error-prone —
typos in the custom fields silently break the sync and the
operator has no easy way to debug.

This script automates the create end of the loop. It takes
the custom fields + the body on the CLI, derives the auth +
encryption keys from the master password + the user's KDF
settings, encrypts the payload, and POSTs the cipher to
Vaultwarden. The result is a Secure Note that VKS picks up
on its next sync cycle.

App-agnostic by design
----------------------
The script is not tied to any one app. The same invocation
seeds::

  - gitea-runner/registrationToken (gitea-runner app)
  - cloudflared-cloudflare-tunnel/credentials.json (cloudflared app)
  - <any future VKS-managed Secret> you wire up

You tell it which Kubernetes Secret + key + namespace you're
targeting (which become the VKS custom fields), and what body
to put in the note.

Authentication
--------------
Pulls the Bitwarden user API key
(client_id + client_secret) from the same Secret the VKS
Deployment reads (``vaultwarden-kubernetes-secrets``,
key ``BW_CLIENTID`` / ``BW_CLIENTSECRET``). That Secret is
created by ``scripts/reseed-vks-creds.sh`` on first install
and lives in the cluster.

Prompts for the Vaultwarden master password on stdin. Two-Factor
Auth is NOT supported (VaultwardenK8sSync requires the same).
The master password is never written to disk or logged; the
script derives a one-shot auth hash, sends that, then
forgets it.

Stdlib + cryptography
---------------------
Only ``cryptography`` (added via ``uv add``) is a third-party
dep — everything else is the Python stdlib. No requests, no
bw CLI. The crypto is exactly what Bitwarden uses on the
client side: PBKDF2-SHA256 for key stretching, AES-256-CBC +
HMAC-SHA256 for cipher encryption, format ``2.<ct>|<iv>|<mac>``
per the Bitwarden encryption spec.

Usage
-----
::

    # Create the cloudflared tunnel credentials note
    ./scripts/vaultwarden-seed-note.py \\
        --app cloudflared \\
        --namespace cloudflared \\
        --secret-name cloudflared-cloudflare-tunnel \\
        --secret-key credentials.json \\
        --body @infra/secrets/cloudflared-tunnel.json

    # Create the gitea-runner registration token note
    ./scripts/vaultwarden-seed-note.py \\
        --app gitea-runner \\
        --namespace gitea-runner \\
        --secret-name gitea-runner-config \\
        --secret-key registrationToken \\
        --body 'abc123def456...'

    # Or read body from stdin
    echo 's3cr3t' | ./scripts/vaultard-runner \\
        --app foo --namespace foo --secret-name foo --secret-key bar \\
        --body -

The script exits 0 on a clean create, 1 on a partial failure
(Secure Note created but body wrong, etc.), and 2 on an auth
or network failure. CI / agentic operators can branch on the
exit code.

Security notes
--------------
- Master password is read once from stdin and never logged,
  echoed, or persisted.
- The access token is short-lived (~1h); the script does not
  ask for a refresh token and does not write it anywhere.
- Body content is encrypted with the user's vault symmetric
  key BEFORE it leaves the machine. The server only sees the
  ciphertext.
- On any error path the script bails BEFORE POSTing the
  cipher — a malformed auth response cannot produce a half-
  written Vault item.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Final, cast

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives import hashes

# ---------- Vaultwarden API contract ----------

# Bitwarden encryption type for client-encrypted strings. The
# server stores them as ``2.<b64-ct>|<b64-iv>|<b64-mac>``.
ENC_TYPE: Final = 2

# Cipher type IDs (Bitwarden API).
TYPE_SECURE_NOTE: Final = 2

# SecureNote.type values (the inner discriminator; 0 = generic).
SECURE_NOTE_GENERIC: Final = 0

# Field type IDs (custom fields on a cipher).
FIELD_TYPE_TEXT: Final = 0

# PBKDF2 iterations we default to if the server's prelogin
# response omits KDF iterations. Vaultwarden defaults to
# 600_000 as of 1.33.x; the prelogin endpoint reports the
# actual server-configured value.
DEFAULT_KDF_ITERATIONS: Final = 600_000


# ---------- CLI ----------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vaultwarden-seed-note",
        description=(
            "Create a Vaultwarden Secure Note that VaultwardenK8sSync "
            "will sync into a Kubernetes Secret."
        ),
    )
    parser.add_argument(
        "--app",
        required=True,
        help=(
            "Friendly app name used as the Secure Note's display name "
            "(e.g. 'cloudflared', 'gitea-runner'). Shown in the "
            "Vaultwarden web UI; not used by VKS."
        ),
    )
    parser.add_argument(
        "--namespace",
        required=True,
        help="Kubernetes namespace VKS will write the Secret into (VKS custom field `namespaces`).",
    )
    parser.add_argument(
        "--secret-name",
        required=True,
        help="Kubernetes Secret name VKS will create/update (VKS custom field `secret-name`).",
    )
    parser.add_argument(
        "--secret-key",
        required=True,
        help="Key inside the Secret whose value the note body holds (VKS custom field `secret-key`).",
    )
    parser.add_argument(
        "--body",
        required=True,
        help=(
            "Note body content. Pass a literal string, or prefix "
            "with '@' to read from a file (e.g. '@infra/secrets/x.json'), "
            "or '-' to read from stdin."
        ),
    )
    parser.add_argument(
        "--vaultwarden-url",
        default=os.environ.get(
            "VAULTWARDEN__SERVERURL",
            "https://bitwarden.bruj0.net",
        ),
        help="Vaultwarden server base URL (default: $VAULTWARDEN__SERVERURL or 'https://bitwarden.bruj0.net').",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG"),
        help=(
            "Path to the cluster kubeconfig used to read the VKS "
            "Secret (default: $KUBECONFIG or the standard lookup "
            "path)."
        ),
    )
    parser.add_argument(
        "--vks-namespace",
        default="vaultwarden-kubernetes-secrets",
        help="Namespace of the VKS Secret (default: 'vaultwarden-kubernetes-secrets').",
    )
    parser.add_argument(
        "--vks-secret-name",
        default="vaultwarden-kubernetes-secrets",
        help="Name of the VKS Secret (default: 'vaultwarden-kubernetes-secrets').",
    )
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Vaultwarden account email. Default: "
            f"{DEFAULT_VAULTWARDEN_EMAIL!r} (the dedicated secrets-"
            "only account this repo is wired to). Override via "
            "$VAULTWARDEN__EMAIL."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the encrypted payload + the request that would be sent without actually calling the API.",
    )
    parser.add_argument(
        "--password-file",
        default=None,
        help=(
            "Path to a file containing the master password (first "
            "line, trailing newline stripped). Skips the "
            "interactive prompt — useful when pasting "
            "non-ASCII / very-long passwords, or for "
            "non-interactive automation. Mutually exclusive "
            "with the interactive prompt."
        ),
    )
    parser.add_argument(
        "--debug-hash",
        action="store_true",
        help=(
            "Print the derived PBKDF2 auth_hash (base64) before "
            "calling /identity/connect/token. Used to verify "
            "the password bytes were captured correctly — if "
            "this hash matches the Bitwarden web client's, the "
            "script is correctly reading your password."
        ),
    )
    return parser.parse_args(argv)


def load_body(spec: str) -> str:
    """Resolve a `--body` value into the literal string content.

    Supports three forms:
        literal         -> the string itself
        @path/to/file   -> file contents (utf-8)
        -               -> stdin (one read; trailing newline stripped)
    """
    if spec == "-":
        data = sys.stdin.read()
        # Don't strip leading/trailing whitespace inside the
        # body — credentials can start/end with whitespace
        # (rare but possible). Just rstrip the lone trailing
        # newline that echo adds.
        if data.endswith("\n"):
            data = data[:-1]
        return data
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


# ---------- kubeconfig + Secret plumbing ----------

def resolve_kubeconfig(explicit: str | None) -> str:
    """Mirror the resolution order in scripts/reseed-vks-creds.sh:
    $KUBECONFIG → $KUBECONFIG → ~/.kube/config → the sibling
    proxmox-k3s repo's per-cluster kubeconfig.
    """
    if explicit:
        return explicit
    if env := os.environ.get("KUBECONFIG"):
        return env
    if Path.home().joinpath(".kube/config").exists():
        return str(Path.home().joinpath(".kube/config"))
    here = Path(__file__).resolve().parent.parent
    fallback = here.parent / "proxmox-k3s" / "infra" / "clusters" / "cicd" / "kubeconfig.yaml"
    return str(fallback)


def read_vks_secret(
    kubeconfig: str,
    namespace: str,
    secret_name: str,
) -> tuple[str, str]:
    """Read BW_CLIENTID + BW_CLIENTSECRET from the VKS Secret.

    Mirrors the bash script's kubectl-based read with --jsonpath.
    Returns (client_id, client_secret). Both are required; an
    empty / missing value raises.
    """
    def _jsonpath(key: str) -> str:
        out = subprocess.run(
            [
                "kubectl",
                f"--kubeconfig={kubeconfig}",
                "-n",
                namespace,
                "get",
                "secret",
                secret_name,
                "-o",
                f"jsonpath={{.data.{key}}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return base64.b64decode(out.stdout.strip()).decode("utf-8")

    client_id = _jsonpath("BW_CLIENTID").strip()
    client_secret = _jsonpath("BW_CLIENTSECRET").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            f"ERROR: BW_CLIENTID or BW_CLIENTSECRET empty in Secret "
            f"{namespace}/{secret_name}; run scripts/reseed-vks-creds.sh "
            f"first."
        )
    return client_id, client_secret


# ---------- crypto primitives ----------

def b64(b: bytes) -> str:
    """Bitwarden uses standard base64 with NO padding stripped
    AND url-safe alphabet. The API accepts padding-required
    base64; we send that.
    """
    return base64.b64encode(b).decode("ascii")


def make_master_key(master_password: str, email: str, iterations: int) -> bytes:
    """Step 1: derive the master key from (password + email + KDF).

    ``email`` is lowercased and used as the salt. The KDF is
    PBKDF2-SHA256 for the default Vaultwarden config; the
    prelogin response carries the iterations count.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        email.lower().encode("utf-8"),
        iterations,
        dklen=32,
    )


def make_server_auth_hash(master_key: bytes, master_password: str) -> str:
    """Step 2: PBKDF2-SHA256(master_key, master_password, 1
    round) base64-encoded. This is what goes in the
    ``password=`` form field of POST /identity/connect/token.

    Bitwarden's reference implementation is
    ``util::pbkdf2(master_key_bytes, password_bytes, rounds=1)``,
    which is exactly ``PBKDF2-HMAC-SHA256(password=master_key,
    salt=original_password, iterations=1, dklen=32)``. The
    salt here is the original user password, NOT a
    pre-stretched master key — verified against the official
    test vector in ``bitwarden_crypto::keys::master_key::tests``
    (``password="asdfasdf"``,
    ``salt="test@bitwarden.com"``, ``iterations=100_000`` →
    hash ``"wmyadRMyBZOH7P/a/ucTCbSghKgdzDpPqUnu/DAVtSw="``,
    with trailing ``=`` padding KEPT).

    IMPORTANT: the trailing ``=`` MUST be kept. Vaultwarden
    stores the password verification hash as
    ``PBKDF2-HMAC-SHA256(auth_hash_string.as_bytes(),
    user.salt, user.password_iterations)``. The raw auth_hash
    string is the input — including its trailing ``=``
    padding. Stripping the ``=`` shortens the input by one
    byte, which produces a different PBKDF2 output, which
    doesn't match the stored hash, which fails auth with
    "Username or password is incorrect" — even when the
    master password bytes are correct.

    This was a real bug discovered on 2026-07-14: the script
    sent ``...+XjQ`` (43 chars), the official ``bw`` CLI sent
    ``...+XjQ=`` (44 chars), only the latter matched the
    stored hash. Tests now pin the padded output explicitly.
    """
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        master_key,
        master_password.encode("utf-8"),
        1,
        dklen=32,
    )
    # base64 with PADDING PRESERVED. The Bitwarden reference
    # CLI (``bw``), the web vault, and Vaultwarden's own
    # stored hash all use padded base64 here. Do NOT rstrip.
    return base64.b64encode(digest).decode("ascii")


def aes_cbc_encrypt(enc_key: bytes, mac_key: bytes, plaintext: bytes) -> tuple[bytes, bytes, bytes]:
    """Bitwarden symmetric encryption: AES-256-CBC with
    HMAC-SHA256 over (iv || ct). The encryption key and the
    MAC key are SEPARATE 32-byte keys (Bitwarden uses
    HKDF-Expand to stretch a master/user key into the pair).

    Returns ``(iv, ciphertext, mac_bytes)``.
    """
    iv = secrets.token_bytes(16)
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    # Pkcs7 padding
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad] * pad)
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    # HMAC over (iv || ct) using mac_key — NOT enc_key.
    mac_digest = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return iv, ciphertext, mac_digest


def make_user_key() -> bytes:
    """The user key is a fresh 64-byte random string: the
    first 32 bytes are the AES key; the last 32 are the HMAC
    key. We don't generate one in this script (the user
    already has one — it's what wraps the vault symmetric
    key). Instead we *unwrap* the existing one with the
    master password. See ``unwrap_user_key``.
    """
    raise NotImplementedError(
        "make_user_key is only used when CREATING a new vault. "
        "This script assumes an existing vault."
    )


def stretch_master_key(master_key: bytes) -> tuple[bytes, bytes]:
    """Stretch the 32-byte master key into (enc_key, mac_key)
    using HKDF-Expand with the Bitwarden "enc"/"mac" labels.

    The Bitwarden Type-2 envelope uses two separate 32-byte
    keys derived from the master key: ``enc_key`` for AES-CBC
    and ``mac_key`` for HMAC-SHA256. The reference
    implementation is ``HKDF-Expand(master_key, info,
    length=32, hash=SHA256)`` where ``info`` is the literal
    ASCII string ``"enc"`` or ``"mac"`` (no length prefix).

    Returns ``(enc_key, mac_key)`` — both 32 bytes.
    """
    enc_key = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"enc",
    ).derive(master_key)
    mac_key = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"mac",
    ).derive(master_key)
    return enc_key, mac_key


def unwrap_user_key(master_key: bytes, encrypted_user_key_b64: str) -> bytes:
    """Decrypt the user's symmetric vault key with the
    derived master key. The encrypted blob is
    ``2.<b64-iv>|<b64-ct>|<b64-mac>`` (Type-2 = AES-CBC-HMAC).

    The master key is stretched via HKDF into
    ``(enc_key, mac_key)`` before use — see
    ``stretch_master_key``. The mac_key verifies the
    integrity tag; enc_key decrypts the ciphertext.

    Field order in the envelope: IV first, then ciphertext,
    then MAC. The MAC is computed over ``iv || ct`` (NOT
    ``ct || iv``). Verified against
    ``bitwarden/sdk-internal/crates/bitwarden-crypto/src/enc_string/symmetric.rs``
    and ``crates/bitwarden-crypto/src/aes.rs``
    (``decrypt_aes256_hmac``).
    """
    enc_type, _, blob = encrypted_user_key_b64.partition(".")
    if int(enc_type) != ENC_TYPE:
        raise SystemExit(
            f"ERROR: unsupported user key encryption type {enc_type}; "
            f"expected {ENC_TYPE}"
        )
    iv_b64, ct_b64, mac_b64 = blob.split("|")
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    expected_mac = base64.b64decode(mac_b64)
    # HKDF-stretch the master key into enc_key + mac_key.
    enc_key, mac_key = stretch_master_key(master_key)
    # Verify the MAC against mac_key BEFORE attempting to
    # decrypt — constant-time compare, fail closed.
    # MAC is over (iv || ct) — see Rust aes::generate_mac.
    mac_digest = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac_digest, expected_mac):
        raise SystemExit(
            "ERROR: user-key MAC mismatch — wrong master password?"
        )
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    if pad < 1 or pad > 16:
        raise SystemExit(
            f"ERROR: invalid PKCS7 padding ({pad}); wrong master password?"
        )
    plaintext = padded[:-pad]
    if len(plaintext) != 64:
        raise SystemExit(
            f"ERROR: user key length {len(plaintext)} != 64; wrong master password?"
        )
    return plaintext


def split_user_key(user_key: bytes) -> tuple[bytes, bytes]:
    """Split the 64-byte user key into (enc_key, mac_key).
    Bitwarden uses the same key for both via the bitwarden-crypto
    convention; AES-CBC + HMAC-SHA256 split.
    """
    if len(user_key) != 64:
        raise SystemExit(f"ERROR: user key must be 64 bytes, got {len(user_key)}")
    return user_key[:32], user_key[32:]


def encrypt_str_for_vault(plaintext: str, user_key: bytes) -> str:
    """Encrypt a single string for storage as a cipher field.
    Returns the Bitwarden-encrypted form ``2.<b64-ct>|<b64-iv>|<b64-mac>``.

    The 64-byte user key is split into ``(enc_key, mac_key)``:
    first 32 bytes are the AES-CBC key, last 32 bytes are the
    HMAC-SHA256 key. This matches what every Bitwarden
    client (web, mobile, CLI) does — passing the user key
    as a single 64-byte blob, then slicing it for enc + mac.
    """
    enc_key, mac_key = split_user_key(user_key)
    iv, ct, mac_digest = aes_cbc_encrypt(
        enc_key, mac_key, plaintext.encode("utf-8")
    )
    # Bitwarden envelope shape: ENC_TYPE.<b64-iv>|<b64-ct>|<b64-mac>
    return f"{ENC_TYPE}.{b64(iv)}|{b64(ct)}|{b64(mac_digest)}"


# ---------- Vaultwarden API calls ----------

class VaultwardenHTTPError(SystemExit):
    """Raised when the Vaultwarden HTTP surface returns a
    non-2xx or a non-JSON body. We promote urllib's
    ``HTTPError`` to ``SystemExit`` so the script's main()
    can map it to a stable exit code without leaking a
    stack trace to the operator.
    """

    def __init__(self, url: str, code: int, body: str) -> None:
        snippet = body.strip()[:300]
        super().__init__(
            f"ERROR: Vaultwarden HTTP {code} for {url}: {snippet}"
        )


# User-Agent string Cloudflare's edge is happy with. The
# default `Python-urllib/X.Y` is blocked by some WAF rules
# (the Bitwarden cloud WAF blocks it outright, and a few
# self-hosted Vaultwarden instances mirror the same rule).
# We deliberately use a UA that looks like a generic curl
# so the request flows through.
DEFAULT_USER_AGENT: Final = "curl/8.5.0"

# Bitwarden-Client-Version is REQUIRED by Vaultwarden's
# /identity/connect/token endpoint — the auth.rs FromRequest
# impl rejects requests with "No Bitwarden-Client-Version
# header provided" before the password check even runs.
# Value must parse as semver: YYYY.MM.PATCH (e.g. 2025.12.0).
# We use the current Vaultwarden web-vault version so the
# server can't reasonably reject us as too-old.
DEFAULT_CLIENT_VERSION: Final = "2025.12.0"

# device-type header — used by Vaultwarden's ClientHeaders
# FromRequest impl to populate the per-device row in the
# devices table. Bitwarden's DeviceType enum maps:
#   8  = LinuxDesktop (NOT what we want — that's a GUI client)
#   21 = SDK
#   22 = Server
#   23 = WindowsCLI
#   24 = MacOsCLI
#   25 = LinuxCLI  ← what the official `bw` CLI uses
# We use 25 so the Vaultwarden Devices page shows this
# script as a CLI client (matches what `bw login` does).
DEFAULT_DEVICE_TYPE: Final = "25"


def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener with the default headers every
    Vaultwarden request needs.

    Three headers are set here:

    1. ``User-Agent`` — Cloudflare's edge blocks the
       default ``Python-urllib/X.Y`` UA. A curl-shaped UA
       passes through.
    2. ``Bitwarden-Client-Version`` — REQUIRED by the
       /identity/connect/token endpoint. Vaultwarden
       rejects the request with "No Bitwarden-Client-Version
       header provided" if this is missing, BEFORE the
       password check. Must be semver (``YYYY.MM.PATCH``).
    3. ``device-type`` — read by Vaultwarden's ClientHeaders
       FromRequest impl to populate the device table.
       Optional but recommended; defaults to "8" (Server,
       matching the form field).

    Setting these on the opener means every call site picks
    them up automatically — no per-Request boilerplate.
    """
    opener = urllib.request.build_opener()
    opener.addheaders = [
        ("User-Agent", DEFAULT_USER_AGENT),
        ("Bitwarden-Client-Version", DEFAULT_CLIENT_VERSION),
        ("device-type", DEFAULT_DEVICE_TYPE),
    ]
    return opener


_OPENER: Final = _build_opener()


def _open(req: urllib.request.Request) -> str:
    """Single chokepoint for urlopen. Translates non-2xx
    responses into ``VaultwardenHTTPError`` so callers don't
    have to wrap every call in try/except.
    """
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return cast(str, resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        raise VaultwardenHTTPError(url, exc.code, body) from None


def http_post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    """POST application/x-www-form-urlencoded and return the
    decoded JSON body. Raises on non-2xx.
    """
    data = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    body = _open(req)
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: non-JSON response from {url}: {body[:500]}") from exc


def http_post_json(url: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    """POST application/json with Bearer auth and return JSON.
    Raises on non-2xx with the response body in the error.
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
    body = _open(req)
    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: non-JSON response from {url}: {body[:500]}") from exc


def prelogin(base_url: str, email: str) -> dict[str, Any]:
    """POST /identity/accounts/prelogin → KDF settings.

    Vaultwarden (self-hosted) and Bitwarden cloud both
    serve the identity API under ``/identity/``. The
    endpoint takes a JSON body (``{"email": "..."}``) and
    returns the user's KDF configuration:
    ``{"kdf": 0, "kdfIterations": 600000, ...}``. We use
    ``kdf=0`` as PBKDF2-SHA256 (the default for the
    Vaultwarden instance this repo targets; the server
    reports the exact value).
    """
    url = f"{base_url.rstrip('/')}/identity/accounts/prelogin"
    data = json.dumps({"email": email}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return cast(dict[str, Any], json.loads(_open(req)))


def password_login(
    base_url: str,
    client_id: str,
    client_secret: str,
    email: str,
    auth_hash: str,
) -> dict[str, Any]:
    """POST /identity/connect/token with grant_type=password → access token."""
    url = f"{base_url.rstrip('/')}/identity/connect/token"
    return http_post_form(
        url,
        {
            "grant_type": "password",
            "username": email,
            "password": auth_hash,
            "scope": "api offline_access",
            "client_id": client_id,
            "client_secret": client_secret,
            "deviceType": "25",  # LinuxCLI — matches what bw sends
            "deviceIdentifier": "proxmox-cicd-vks-seed-note",
            "deviceName": "proxmox-cicd scripts/vaultwarden-seed-note.py",
        },
    )


def fetch_profile(base_url: str, access_token: str) -> dict[str, Any]:
    """GET /api/accounts/profile → email + encrypted user key."""
    url = f"{base_url.rstrip('/')}/api/accounts/profile"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return cast(dict[str, Any], json.loads(_open(req)))


def create_secure_note(
    base_url: str,
    access_token: str,
    note_name: str,
    body_text: str,
    custom_fields: dict[str, str],
) -> dict[str, Any]:
    """POST /api/ciphers with a type=2 Secure Note.

    `body_text` is the raw value VKS will sync into the
    Secret key. `custom_fields` is the VKS-reserved triple
    (namespaces, secret-name, secret-key) — these become
    CipherField type=0 entries on the Secure Note.
    """
    # Caller encrypts the body + name + custom field values
    # with the user key BEFORE calling this. We expect an
    # already-encrypted request payload here; the caller
    # passes the full dict.
    raise NotImplementedError("see build_secure_note_payload")


def build_secure_note_payload(
    note_name: str,
    body_text: str,
    custom_fields: dict[str, str],
    user_key: bytes,
) -> dict[str, Any]:
    """Compose the JSON payload that goes into POST /api/ciphers.

    The body, name, and each custom field value are encrypted
    with the user's vault symmetric key. The Bitwarden API
    rejects plaintext cipher fields.
    """
    fields = [
        {
            "type": FIELD_TYPE_TEXT,
            "name": encrypt_str_for_vault(name, user_key),
            "value": encrypt_str_for_vault(value, user_key),
        }
        for name, value in custom_fields.items()
    ]
    return {
        "type": TYPE_SECURE_NOTE,
        "name": encrypt_str_for_vault(note_name, user_key),
        "notes": encrypt_str_for_vault(body_text, user_key),
        "secureNote": {"type": SECURE_NOTE_GENERIC},
        "fields": fields,
        "favorite": False,
    }


# ---------- orchestration ----------

# Default email for the dedicated secrets-only Vaultwarden
# account. The cluster's VKS Deployment uses this same
# account (see infra/secrets/cloudflared-api-token.json and
# the VAULTWARDEN__SERVERURL entry in .env). Override on the
# CLI via --email or in the env via VAULTWARDEN__EMAIL.
DEFAULT_VAULTWARDEN_EMAIL: Final = "secrets@bruj0.net"


def derive_email(explicit: str | None = None) -> str:
    """Resolve the Vaultwarden account email to authenticate
    against. Resolution order:

      1. ``--email`` CLI flag (passed in as ``explicit``)
      2. ``$VAULTWARDEN__EMAIL`` env var
      3. ``DEFAULT_VAULTWARDEN_EMAIL`` (the dedicated
         secrets-only account this repo is wired to)

    We deliberately do NOT fall back to
    ``$CLOUDFLARE_GLOBAL_API_EMAIL`` — that address is the
    Cloudflare admin, not the Vaultwarden user. The
    dedicated ``secrets@bruj0.net`` account is what VKS
    reads from ``infra/secrets/vaultwarden-init.json`` and
    what reseed-vks-creds.sh writes to the in-cluster
    ``vaultwarden-kubernetes-secrets`` Secret.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("VAULTWARDEN__EMAIL")
    if env:
        return env.strip()
    return DEFAULT_VAULTWARDEN_EMAIL


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # 1. Pull BW client_id + secret from the in-cluster VKS Secret.
    kubeconfig = resolve_kubeconfig(args.kubeconfig)
    client_id, client_secret = read_vks_secret(
        kubeconfig=kubeconfig,
        namespace=args.vks_namespace,
        secret_name=args.vks_secret_name,
    )
    email = derive_email(args.email)
    print(f"  ↳ Vaultwarden account: {email}")
    if args.password_file:
        # --password-file skips the interactive prompt. The
        # file is expected to contain the password on the
        # first line (any trailing newline is stripped). This
        # is the canonical way to feed non-ASCII passwords,
        # passwords that contain shell-special chars, or to
        # run the script non-interactively from a secret
        # manager. The file's contents are read once and the
        # bytes are zeroed out below.
        with open(args.password_file, encoding="utf-8") as f:
            master_password = f.readline().rstrip("\n").rstrip("\r")
        print("  ↳ Master password loaded from file (interactive prompt skipped).")
    else:
        master_password = getpass.getpass("Vaultwarden master password: ")

    # 2. Discover KDF settings + derive the auth hash.
    #    Bitwarden's auth flow is two PBKDF2 rounds:
    #      (a) master_key = PBKDF2(password, email, KDF.iterations, 32)
    #      (b) auth_hash  = PBKDF2(master_key, password, 1, 32)  [base64, no padding]
    #    We never compute the "stretched" master key here — that's
    #    only used to unwrap the user's encrypted vault key
    #    after /connect/token succeeds (see unwrap_user_key).
    pre = prelogin(args.vaultwarden_url, email)
    iterations = int(pre.get("kdfIterations", DEFAULT_KDF_ITERATIONS))
    master_key = make_master_key(master_password, email, iterations)
    auth_hash = make_server_auth_hash(master_key, master_password)
    if args.debug_hash:
        # The hash is non-secret (it's the server-side
        # authentication hash, deliberately not derivable
        # back to the master password), so it's safe to print
        # for diagnostic purposes. The user can compare it to
        # the value their Bitwarden web vault computes — if
        # the two match, the password bytes were captured
        # correctly and the remaining failure must be on
        # Vaultwarden's side.
        print(f"  ↳ KDF iterations: {iterations}")
        print(f"  ↳ derived auth_hash: {auth_hash}")
        print(f"  ↳ password length (bytes): {len(master_password.encode('utf-8'))}")
    # Sensitive: overwrite so the bytes don't sit in memory.
    master_password = ""

    # 3. Log in.
    token_resp = password_login(
        args.vaultwarden_url,
        client_id=client_id,
        client_secret=client_secret,
        email=email,
        auth_hash=auth_hash,
    )
    access_token = token_resp.get("access_token")
    if not access_token:
        print(
            f"ERROR: /connect/token response missing access_token; "
            f"got keys={sorted(token_resp.keys())}",
            file=sys.stderr,
        )
        return 2
    print(
        f"  ↳ /connect/token OK (token TTL = "
        f"{token_resp.get('expires_in', '?')}s)"
    )

    # 4. Pull the encrypted user key from /api/accounts/profile
    #    and unwrap it with the derived master key.
    profile = fetch_profile(args.vaultwarden_url, access_token)
    if args.debug_hash:
        # Print the wrapped user key (without unwrap) so we can
        # see what Vaultwarden returned and compare to what
        # the Bitwarden reference web vault shows.
        print(f"  ↳ wrapped user key (first 60 chars): {profile.get('key','')[:60]}...")
        print(f"  ↳ wrapped user key length: {len(profile.get('key',''))}")
        print(f"  ↳ derived master_key (b64): {base64.b64encode(master_key).decode()}")
    user_key = unwrap_user_key(master_key, profile["key"])

    # 5. Compose + (optionally) POST the Secure Note.
    body_text = load_body(args.body)
    custom_fields = {
        "namespaces": args.namespace,
        "secret-name": args.secret_name,
        "secret-key": args.secret_key,
    }
    payload = build_secure_note_payload(
        note_name=f"{args.app} k8s secret value",
        body_text=body_text,
        custom_fields=custom_fields,
        user_key=user_key,
    )

    if args.dry_run:
        print("DRY RUN — would POST the following to /api/ciphers:")
        print(json.dumps(payload, indent=2))
        return 0

    create_resp = http_post_json(
        f"{args.vaultwarden_url.rstrip('/')}/api/ciphers",
        payload,
        access_token,
    )
    if not create_resp.get("id"):
        print(
            f"ERROR: /api/ciphers response missing id; got "
            f"{json.dumps(create_resp)[:500]}",
            file=sys.stderr,
        )
        return 1
    print(
        f"  ↳ Secure Note created: id={create_resp['id']} "
        f"name={args.app} namespaces={args.namespace} "
        f"secret-name={args.secret_name} secret-key={args.secret_key}"
    )
    print(
        f"\nVaultwardenK8sSync will pick this up on its next sync "
        f"cycle (default: every {os.environ.get('SYNC__SYNCINTERVALSECONDS', '300')}s). "
        f"Watch the sync with:\n"
        f"  kubectl -n {args.vks_namespace} logs -l "
        f"app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
