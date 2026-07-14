#!/usr/bin/env python3
"""scripts/vaultwarden-notes.py — Vaultwarden Secure Note CLI.

A multi-command interface to the Vaultwarden / Bitwarden
REST API, built on top of the ``provisioner.lib.vaultwarden``
package. Subcommands:

  - ``seed``     Create (or idempotently update) a Secure
                 Note whose custom fields are the VKS triple
                 ``namespaces / secret-name / secret-key``
                 and whose body is the raw value VKS writes
                 into that Secret key. This is the canonical
                 way to feed the orchestrator's managed
                 Secrets into VaultwardenK8sSync.
  - ``delete``   DELETE one or more Secure Notes by id, or
                 by ``--match name`` substring against the
                 decrypted name.
  - ``list``     List all ciphers (org + personal), printing
                 decrypted names. Filterable by org / folder.
  - ``decrypt``  Decrypt a single field's value from a
                 cipher. Useful for inspecting what VKS
                 currently sees.

Authentication
--------------
Pulls BW_CLIENTID + BW_CLIENTSECRET from the in-cluster
Secret the VKS Deployment reads (default
``vaultwarden-kubernetes-secrets`` / ``vaultwarden-kubernetes-secrets``
in namespace ``vaultwarden-kubernetes-secrets``). Override
with ``--vks-namespace`` / ``--vks-secret-name``.

Prompts for the Vaultwarden master password on stdin. Use
``--password-file <path>`` to feed it from a file (e.g.
``/tmp/vw.pw``) and skip the interactive prompt.

Stdlib + cryptography only — no requests, no bw CLI. The
crypto is exactly what Bitwarden uses: PBKDF2-SHA256 for
key stretching, AES-256-CBC + HMAC-SHA256 for envelope
encryption, ``2.<ct>|<iv>|<mac>`` wire shape.

Exit codes:
  0  success
  1  Vaultwarden rejected the request (HTTP 4xx)
  2  auth or network failure
  3  usage / argument error
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from provisioner.lib.vaultwarden import (
    VaultwardenClient,
    build_secure_note_payload,
    decrypt_str_from_vault,
    vks_triple,
)
from provisioner.lib.vaultwarden.kubeconfig import resolve_kubeconfig


# Default email for the dedicated secrets-only Vaultwarden
# account. Matches the in-cluster VKS Deployment's
# BW_CLIENTID Secret entry. Override via --email or
# $VAULTWARDEN__EMAIL.
DEFAULT_VAULTWARDEN_EMAIL = "secrets@bruj0.net"


def _read_vks_secret(
    kubeconfig: str,
    namespace: str,
    secret_name: str,
) -> tuple[str, str]:
    """Read BW_CLIENTID + BW_CLIENTSECRET from a k8s Secret.

    Mirrors the bash script's kubectl-based read with
    --jsonpath. Returns (client_id, client_secret).
    """
    import base64
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


def _load_body(spec: str) -> str:
    """Resolve a `--body` value into the literal string content.

    Supports three forms:
        literal        -> the string itself
        @path/to/file  -> file contents (utf-8)
        -              -> stdin (one read; trailing newline stripped)
    """
    if spec == "-":
        data = sys.stdin.read()
        if data.endswith("\n"):
            data = data[:-1]
        return data
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


def _resolve_email(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("VAULTWARDEN__EMAIL")
    if env:
        return env.strip()
    return DEFAULT_VAULTWARDEN_EMAIL


def _build_client(args: argparse.Namespace) -> VaultwardenClient:
    """Resolve auth + log in. Returns a ready-to-use client."""
    kubeconfig = resolve_kubeconfig(args.kubeconfig)
    client_id, client_secret = _read_vks_secret(
        kubeconfig=kubeconfig,
        namespace=args.vks_namespace,
        secret_name=args.vks_secret_name,
    )
    email = _resolve_email(args.email)
    print(f"  ↳ Vaultwarden account: {email}")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as f:
            master_password = f.readline().rstrip("\n").rstrip("\r")
        print("  ↳ Master password loaded from file (interactive prompt skipped).")
    else:
        master_password = getpass.getpass("Vaultwarden master password: ")
    print("  ↳ /identity/connect/token …")
    client = VaultwardenClient.login(
        server_url=args.vaultwarden_url,
        client_id=client_id,
        client_secret=client_secret,
        email=email,
        master_password=master_password,
    )
    # Overwrite master_password bytes before returning.
    master_password = ""
    print("  ↳ user key unwrapped OK")
    return client


# ============================================================================
# Subcommands
# ============================================================================


def cmd_seed(args: argparse.Namespace) -> int:
    """Create (or idempotently update) a Secure Note."""
    client = _build_client(args)
    body_text = _load_body(args.body)
    custom_fields = vks_triple(
        namespace=args.namespace,
        secret_name=args.secret_name,
        secret_key=args.secret_key,
    )
    note_name = f"{args.app} k8s secret value"
    payload = build_secure_note_payload(
        note_name=note_name,
        body_text=body_text,
        custom_fields=custom_fields,
        user_key=client.user_key,
    )
    if args.dry_run:
        print("DRY RUN — would POST the following to /api/ciphers:")
        print(json.dumps(payload, indent=2))
        return 0
    resp = client.create_cipher(payload)
    if not resp.get("id"):
        print(
            f"ERROR: /api/ciphers response missing id; got "
            f"{json.dumps(resp)[:500]}",
            file=sys.stderr,
        )
        return 1
    print(
        f"  ↳ Secure Note created: id={resp['id']} name={args.app} "
        f"namespaces={args.namespace} secret-name={args.secret_name} "
        f"secret-key={args.secret_key}"
    )
    print(
        f"\nVaultwardenK8sSync will pick this up on its next sync "
        f"cycle (default: every 300s). Watch with:\n"
        f"  kubectl -n {args.vks_namespace} logs -l "
        f"app.kubernetes.io/name=vaultwarden-kubernetes-secrets -f"
    )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """DELETE ciphers by id, or by --match name substring."""
    client = _build_client(args)
    ciphers = client.list_ciphers(organization_id=args.organization_id)
    targets: list[tuple[str, str]] = []
    for c in ciphers:
        if c.get("deletedDate"):
            continue
        try:
            name = client.decrypt_cipher_name(c)
        except Exception:
            name = "<decrypt-failed>"
        if args.id:
            if c["id"] in args.id:
                targets.append((c["id"], name))
            continue
        if args.match:
            if args.match.lower() in name.lower():
                targets.append((c["id"], name))
            continue
        # No filter — list everything (but require --yes to actually delete).
        targets.append((c["id"], name))
    if not targets:
        print("  (no matching ciphers)")
        return 0
    print(f"  ↳ {len(targets)} cipher(s) match:")
    for cid, name in targets:
        print(f"      - {cid}  {name!r}")
    if args.id is None and args.match is None:
        # Listing only — don't delete.
        return 0
    if not args.yes:
        print(
            "\nPass --yes to actually DELETE these ciphers.",
            file=sys.stderr,
        )
        return 1
    for cid, name in targets:
        try:
            client.delete_cipher(cid)
            print(f"  ↳ DELETED {name!r}  (id={cid})")
        except Exception as e:
            print(f"  ↳ FAILED {name!r}  (id={cid}): {e}", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List ciphers (decrypted names + VKS field summary)."""
    client = _build_client(args)
    ciphers = client.list_ciphers(organization_id=args.organization_id)
    print(f"  ↳ {len(ciphers)} ciphers:")
    for c in ciphers:
        if c.get("deletedDate"):
            continue
        try:
            name = client.decrypt_cipher_name(c)
        except Exception as e:
            name = f"<decrypt-failed: {e}>"
        line = f"      - {c['id']}  {name!r}  type={c.get('type')}"
        if c.get("organizationId"):
            line += f"  org={c['organizationId']}"
        print(line)
        # If this is a Secure Note with VKS-shaped fields,
        # decrypt and print the triple too.
        if c.get("type") == 2 and c.get("fields"):
            for f in c["fields"]:
                try:
                    fkey = decrypt_str_from_vault(f["name"], client.user_key)
                    fval = decrypt_str_from_vault(f["value"], client.user_key)
                    print(f"          field: {fkey} = {fval!r}")
                except Exception:
                    pass
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    """Decrypt a single cipher (notes + all fields) by id."""
    client = _build_client(args)
    c = client.get_cipher(args.id)
    try:
        name = client.decrypt_cipher_name(c)
    except Exception as e:
        name = f"<decrypt-failed: {e}>"
    print(f"name: {name!r}")
    try:
        notes = client.decrypt_cipher_notes(c)
        print("notes:")
        print(notes)
    except Exception as e:
        print(f"notes: <decrypt-failed: {e}>")
    for i, f in enumerate(c.get("fields") or []):
        try:
            fname = decrypt_str_from_vault(f["name"], client.user_key)
            fval = decrypt_str_from_vault(f["value"], client.user_key)
            print(f"field[{i}] name={fname!r}  value={fval!r}")
        except Exception as e:
            print(f"field[{i}] <decrypt-failed: {e}>")
    return 0


# ============================================================================
# argparse
# ============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultwarden-notes",
        description="Vaultwarden / Bitwarden Secure Note CLI (seed, delete, list, decrypt).",
    )
    parser.add_argument(
        "--vaultwarden-url",
        default="https://bitwarden.bruj0.net",
        help="Vaultwarden server base URL.",
    )
    parser.add_argument(
        "--kubeconfig",
        default=None,
        help="Path to the kubeconfig used to read the VKS Secret.",
    )
    parser.add_argument(
        "--vks-namespace",
        default="vaultwarden-kubernetes-secrets",
        help="Namespace of the VKS Secret.",
    )
    parser.add_argument(
        "--vks-secret-name",
        default="vaultwarden-kubernetes-secrets",
        help="Name of the VKS Secret.",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Vaultwarden account email (default: secrets@bruj0.net).",
    )
    parser.add_argument(
        "--password-file",
        default=None,
        help="Path to a file containing the master password (first line).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # ---- seed ----
    p_seed = sub.add_parser(
        "seed",
        help="Create a Secure Note for VKS to sync into a k8s Secret.",
    )
    p_seed.add_argument("--app", required=True, help="Friendly app name (used as note display name).")
    p_seed.add_argument("--namespace", required=True, help="k8s namespace (VKS custom field 'namespaces').")
    p_seed.add_argument("--secret-name", required=True, help="k8s Secret name (VKS custom field 'secret-name').")
    p_seed.add_argument("--secret-key", required=True, help="Key inside the Secret (VKS custom field 'secret-key').")
    p_seed.add_argument(
        "--body",
        required=True,
        help="Note body content. Literal string, @path/to/file, or '-' for stdin.",
    )
    p_seed.add_argument("--dry-run", action="store_true", help="Print the encrypted payload without calling the API.")
    p_seed.set_defaults(func=cmd_seed)

    # ---- delete ----
    p_del = sub.add_parser(
        "delete",
        help="Delete Secure Notes by id, or by --match name substring.",
    )
    p_del.add_argument("--id", action="append", default=[], help="Cipher id to delete (repeatable).")
    p_del.add_argument("--match", default=None, help="Substring to match against decrypted cipher names.")
    p_del.add_argument("--organization-id", default=None, help="Restrict to one organization.")
    p_del.add_argument("--yes", action="store_true", help="Actually delete (default: print matches only).")
    p_del.set_defaults(func=cmd_delete)

    # ---- list ----
    p_list = sub.add_parser(
        "list",
        help="List ciphers with decrypted names (and VKS field summary for Secure Notes).",
    )
    p_list.add_argument("--organization-id", default=None, help="Restrict to one organization.")
    p_list.set_defaults(func=cmd_list)

    # ---- decrypt ----
    p_dec = sub.add_parser(
        "decrypt",
        help="Decrypt a single cipher's name, notes, and fields.",
    )
    p_dec.add_argument("--id", required=True, help="Cipher id.")
    p_dec.set_defaults(func=cmd_decrypt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    # The subcommand handlers all return int (set via
    # p_seed.set_defaults(func=cmd_seed) etc.). argparse's
    # Namespace doesn't know that, so cast.
    return cast(int, args.func(args))


if __name__ == "__main__":
    sys.exit(main())
