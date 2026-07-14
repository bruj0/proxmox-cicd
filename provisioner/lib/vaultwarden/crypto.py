"""Bitwarden symmetric crypto primitives.

Implements the **client side** of the Bitwarden encryption
contract: PBKDF2-SHA256 master-key derivation, PBKDF2-SHA256
server-auth hash, HKDF-Expand (enc/mac) key stretching,
and AES-256-CBC + HMAC-SHA256 envelope encrypt/decrypt.

These primitives are byte-for-byte compatible with what
Vaultwarden (and the upstream Bitwarden cloud) expect on
the wire. They are the only dependency between this package
and the cryptography library.

The most subtle rule — and the one that took two debugging
sessions to get right — is the **padding on the server
auth hash**: ``PBKDF2-HMAC-SHA256(master_key, master_password,
1)`` base64-encodes to 44 chars, and the trailing ``=``
MUST be preserved. Vaultwarden's stored password-verification
hash is ``PBKDF2-HMAC-SHA256(auth_hash_string.as_bytes(),
salt, user.password_iterations)`` — the raw auth hash is
the input bytes, including its ``=``. Stripping the
``=`` shortens the input by one byte, changes the PBKDF2
output, and the auth fails with the unhelpful "Username or
password is incorrect".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand


# Type-2 envelope = AES-256-CBC + HMAC-SHA256. Every string
# the Bitwarden client encrypts (cipher name, notes, field
# values, the user's wrapped vault key) carries this prefix.
ENC_TYPE: Final = 2

# Bitwarden's Vaultwarden default KDF iteration count when
# the /identity/accounts/prelogin response omits one. As of
# Vaultwarden 1.33.x the default is 600_000.
DEFAULT_KDF_ITERATIONS: Final = 600_000


def b64(b: bytes) -> str:
    """Standard (NOT urlsafe) base64 with padding preserved.

    The Bitwarden reference CLI ``bw``, the web vault, and
    Vaultwarden all store envelopes with the ``=`` padding
    intact. Use ``b64`` for every ciphertext, IV, MAC, and
    auth-hash encode here — never ``urlsafe_b64encode``,
    and never rstrip the result.
    """
    return base64.b64encode(b).decode("ascii")


def make_master_key(master_password: str, email: str, iterations: int) -> bytes:
    """PBKDF2-SHA256(master_password, lowercased(email), iterations)
    → 32-byte master key.

    The salt is the email address lowercased per the
    Bitwarden reference implementation. Iterations come
    from ``/identity/accounts/prelogin`` and reflect the
    server-configured KDF cost.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        email.lower().encode("utf-8"),
        iterations,
        dklen=32,
    )


def make_server_auth_hash(master_key: bytes, master_password: str) -> str:
    """PBKDF2-SHA256(master_key, master_password, 1) → base64.

    Sent in the ``password=`` form field of POST
    ``/identity/connect/token``. **Trailing ``=`` MUST be
    preserved** — see the module docstring for why.

    Verified against the Bitwarden test vector in
    ``bitwarden_crypto::keys::master_key::tests``:
    password "asdfasdf", salt "test@bitwarden.com",
    iterations 100_000 →
    hash ``"wmyadRMyBZOH7P/a/ucTCbSghKgdzDpPqUnu/DAVtSw="``.
    """
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        master_key,
        master_password.encode("utf-8"),
        1,
        dklen=32,
    )
    return base64.b64encode(digest).decode("ascii")


def aes_cbc_encrypt(
    enc_key: bytes, mac_key: bytes, plaintext: bytes
) -> tuple[bytes, bytes, bytes]:
    """AES-256-CBC encrypt + HMAC-SHA256 over (iv || ct).

    A fresh 16-byte IV is generated on every call (NEVER
    reused across envelopes). PKCS7 padding is applied
    in-line. The MAC is computed over ``iv || ct`` per the
    Bitwarden Rust reference
    (``crates/bitwarden-crypto/src/aes.rs::generate_mac``).

    Returns ``(iv, ciphertext, mac)`` as raw bytes. The
    caller is responsible for base64-encoding each when
    composing the envelope.
    """
    iv = secrets.token_bytes(16)
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad] * pad)
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    mac_digest = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return iv, ciphertext, mac_digest


def aes_cbc_decrypt(
    enc_key: bytes, mac_key: bytes, iv: bytes, ciphertext: bytes, expected_mac: bytes
) -> bytes:
    """AES-256-CBC decrypt + HMAC-SHA256 verify over (iv || ct).

    Returns the unpadded plaintext. Raises ``ValueError`` on
    a MAC mismatch (constant-time compare) or invalid
    PKCS7 padding (any value outside ``[1, 16]``).
    """
    mac_digest = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac_digest, expected_mac):
        raise ValueError(
            "cipher MAC mismatch (wrong key, or tampered ciphertext)"
        )
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1]
    if pad < 1 or pad > 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError(f"invalid PKCS7 padding: {pad}")
    return padded[:-pad]


def stretch_master_key(master_key: bytes) -> tuple[bytes, bytes]:
    """HKDF-Expand-SHA256(master_key, "enc"/"mac", 32) → (enc, mac).

    The Bitwarden Type-2 envelope uses two 32-byte keys
    derived from the same master key. Labels are literal
    ASCII bytes ``b"enc"`` and ``b"mac"`` (no length
    prefix). Verified against the Bitwarden Rust reference
    ``crates/bitwarden-crypto/src/keys.rs``.
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
    """Decrypt the wrapped 64-byte user key with the derived master key.

    The envelope is ``2.<b64-iv>|<b64-ct>|<b64-mac>``. The
    MAC is verified BEFORE decryption (constant-time) and
    the function raises ``ValueError`` on any mismatch.

    Output is exactly 64 bytes: first 32 = enc key, last
    32 = mac key. See ``split_user_key``.
    """
    enc_type, _, blob = encrypted_user_key_b64.partition(".")
    if enc_type and int(enc_type) != ENC_TYPE:
        raise ValueError(
            f"unsupported user key encryption type {enc_type}; "
            f"expected {ENC_TYPE}"
        )
    parts = blob.split("|")
    if len(parts) != 3:
        raise ValueError(
            f"user key envelope must have 3 pipe-separated parts, got {len(parts)}"
        )
    iv_b64, ct_b64, mac_b64 = parts
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    expected_mac = base64.b64decode(mac_b64)
    enc_key, mac_key = stretch_master_key(master_key)
    plaintext = aes_cbc_decrypt(enc_key, mac_key, iv, ct, expected_mac)
    if len(plaintext) != 64:
        raise ValueError(
            f"user key length {len(plaintext)} != 64 (wrong master password?)"
        )
    return plaintext


def split_user_key(user_key: bytes) -> tuple[bytes, mac_key_type]:
    """Split the 64-byte user key into (enc_key, mac_key).

    Bitwarden uses one 64-byte random blob: first 32 bytes
    AES key, last 32 bytes HMAC key. Identical scheme for
    every envelope (cipher name, notes, custom fields,
    organization keys, etc.).
    """
    if len(user_key) != 64:
        raise ValueError(f"user key must be 64 bytes, got {len(user_key)}")
    return user_key[:32], user_key[32:]


# Forward reference: `mac_key_type` is just `bytes`. The
# alias is purely cosmetic so the function signature reads
# the same as the Bitwarden Rust source.
mac_key_type = bytes


def encrypt_str_for_vault(plaintext: str, user_key: bytes) -> str:
    """Encrypt a single string as a Bitwarden Type-2 envelope.

    Returns the canonical ``2.<b64-ct>|<b64-iv>|<b64-mac>``
    form (note: IV first, then CT — this matches the wire
    shape Vaultwarden stores, NOT the ``iv || ct || mac``
    byte order used internally for HMAC verification).
    """
    enc_key, mac_key = split_user_key(user_key)
    iv, ct, mac_digest = aes_cbc_encrypt(
        enc_key, mac_key, plaintext.encode("utf-8")
    )
    return f"{ENC_TYPE}.{b64(iv)}|{b64(ct)}|{b64(mac_digest)}"


def decrypt_str_from_vault(envelope: str, user_key: bytes) -> str:
    """Inverse of ``encrypt_str_for_vault``.

    Accepts both with- and without-prefix envelopes:
      - ``2.<iv>|<ct>|<mac>``
      - ``<iv>|<ct>|<mac>``  (no prefix, legacy)

    Returns the decoded UTF-8 string. Raises ``ValueError``
    on MAC mismatch or padding errors.
    """
    # Detect the optional prefix. The legacy form has no `.`
    # so partition returns (envelope, "", "").
    if "." in envelope and envelope.split(".", 1)[0].isdigit():
        _, _, blob = envelope.partition(".")
    else:
        blob = envelope
    parts = blob.split("|")
    if len(parts) != 3:
        raise ValueError(
            f"envelope must have 3 pipe-separated parts, got {len(parts)}"
        )
    iv_b64, ct_b64, mac_b64 = parts
    enc_key, mac_key = split_user_key(user_key)
    plaintext = aes_cbc_decrypt(
        enc_key,
        mac_key,
        base64.b64decode(iv_b64),
        base64.b64decode(ct_b64),
        base64.b64decode(mac_b64),
    )
    return plaintext.decode("utf-8")
