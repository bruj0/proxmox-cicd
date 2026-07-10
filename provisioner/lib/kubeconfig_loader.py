"""kubeconfig_loader — read the kubeconfig.yaml produced by proxmox-k3s.

The sibling `proxmox-k3s` repo writes
`infra/clusters/<cluster>/kubeconfig.yaml` at the end of a
successful bootstrap. This loader reads it, parses out the
fields the provisioner needs (api_endpoint, default namespace,
and the raw path to pass to `kubectl --kubeconfig`), and
returns a `Kubeconfig` dataclass.

We deliberately do NOT attempt to merge this kubeconfig with
the operator's `~/.kube/config`. The CLI is reproducible on
any operator box: just point `--proxmox-k3s-repo` at the
sibling checkout and the right kubeconfig is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Kubeconfig:
    """Parsed view of proxmox-k3s's infra/clusters/<n>/kubeconfig.yaml."""

    path: Path
    api_endpoint: str
    cluster_name: str
    user_name: str
    context_name: str
    default_namespace: str
    ca_cert_path: str | None


# Pragmatic, narrow YAML parser. The kubeconfig.yaml produced
# by proxmox-k3s is a strict subset of the standard k8s
# kubeconfig shape: only `apiVersion`, `kind`, `clusters`,
# `users`, `contexts`, `current-context`. We don't import
# `pyyaml` to keep the runtime dep surface at zero (mirrors
# proxmox-k3s's hcl_parser pattern).
_CLUSTER_RE = re.compile(
    r"-?\s*cluster:\s*\n(?P<body>(?:[ \t]+.*\n)+)", re.MULTILINE
)
_NAME_RE = re.compile(r"^\s+name:\s*(?P<v>.+)$", re.MULTILINE)
_SERVER_RE = re.compile(r"^\s+server:\s*(?P<v>https?://\S+)$", re.MULTILINE)
_CA_RE = re.compile(r"^\s+certificate-authority-data:\s*(?P<v>\S+)$", re.MULTILINE)
_USER_RE = re.compile(
    r"-?\s*user:\s*\n(?P<body>(?:[ \t]+.*\n)+)", re.MULTILINE
)
_TOKEN_RE = re.compile(r"^\s+token:\s*(?P<v>\S+)$", re.MULTILINE)
_CONTEXT_RE = re.compile(
    r"-?\s*context:\s*\n(?P<body>(?:[ \t]+.*\n)+)", re.MULTILINE
)
_CTX_CLUSTER_RE = re.compile(r"^\s+cluster:\s*(?P<v>\S+)$", re.MULTILINE)
_CTX_USER_RE = re.compile(r"^\s+user:\s*(?P<v>\S+)$", re.MULTILINE)
_CTX_NS_RE = re.compile(r"^\s+namespace:\s*(?P<v>\S+)$", re.MULTILINE)
_CURRENT_CTX_RE = re.compile(
    r"^current-context:\s*(?P<v>\S+)$", re.MULTILINE
)
_API_VERSION_RE = re.compile(r"^apiVersion:\s*(?P<v>\S+)$", re.MULTILINE)
_KIND_RE = re.compile(r"^kind:\s*(?P<v>\S+)$", re.MULTILINE)


class KubeconfigParseError(ValueError):
    """Raised when kubeconfig.yaml is missing or malformed."""


def _top_level_sections(text: str) -> dict[str, str]:
    """Return a {section_name: section_body} dict by splitting
    on top-level keys (lines that don't start with whitespace
    AND don't start with `- `).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        is_top_level = (
            bool(line)
            and not line[0].isspace()
            and not line.startswith("- ")
        )
        if is_top_level:
            # New top-level key: "name: value" or just "name:".
            key = line.split(":", 1)[0].strip()
            current = key
            sections.setdefault(current, []).append(line)
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def _find_blocks(text: str, kind: str) -> list[tuple[str, str]]:
    """Find every block inside the `kind:` section.

    Returns list of (name, full-body). Two shapes are supported:

      Shape A (clusters, contexts):
          - <kind>:
              field: value
            name: cicd

      Shape B (users):
          - name: cicd
            user:
              token: xxx
    """
    section_map = {
        "cluster": "clusters",
        "user": "users",
        "context": "contexts",
    }
    if kind not in section_map:
        raise KubeconfigParseError(f"unknown block kind: {kind}")

    sections = _top_level_sections(text)
    section = sections.get(section_map[kind], "")
    if not section:
        return []

    out: list[tuple[str, str]] = []
    if kind in ("cluster", "context"):
        # Shape A: each entry starts with `- <kind>:`. The
        # matching `name:` line is at the same indent as the
        # `- <kind>:` line.
        pattern = re.compile(
            rf"^- {kind}:\s*\n(?P<body>(?:[ \t]+.+\n?)+)",
            re.MULTILINE,
        )
        for m in pattern.finditer(section):
            body = m.group("body")
            nm = re.search(r"^\s+name:\s*(\S+)\s*$", body, re.MULTILINE)
            if nm:
                out.append((nm.group(1), body))
    else:
        # Shape B: each entry starts with `- name:`.
        pattern = re.compile(
            r"^- name:\s*(?P<n>\S+)\s*\n(?P<body>(?:[ \t]+.+\n?)+)",
            re.MULTILINE,
        )
        for m in pattern.finditer(section):
            out.append((m.group("n"), m.group("body")))
    return out


def load(path: Path) -> Kubeconfig:
    """Read and parse `path`. Raises KubeconfigParseError on miss."""
    if not path.exists():
        raise KubeconfigParseError(
            f"kubeconfig not found at {path}. Did you run `make apply` in the "
            f"sibling proxmox-k3s repo?"
        )

    text = path.read_text(encoding="utf-8")

    # Sanity-check the document shape.
    if not _API_VERSION_RE.search(text) or not _KIND_RE.search(text):
        raise KubeconfigParseError(
            f"kubeconfig at {path} is missing apiVersion/kind; "
            f"does not look like a k8s kubeconfig."
        )

    # Pull the first cluster + user + context. proxmox-k3s writes
    # exactly one of each in its handoff.
    clusters = _find_blocks(text, "cluster")
    users = _find_blocks(text, "user")
    contexts = _find_blocks(text, "context")

    if not clusters:
        raise KubeconfigParseError(f"no `clusters:` block in {path}")
    if not users:
        raise KubeconfigParseError(f"no `users:` block in {path}")
    if not contexts:
        raise KubeconfigParseError(f"no `contexts:` block in {path}")

    cluster_name, cluster_body = clusters[0]
    server_match = _SERVER_RE.search(cluster_body)
    if not server_match:
        raise KubeconfigParseError(
            f"cluster '{cluster_name}' has no `server:` field in {path}"
        )
    api_endpoint = server_match.group("v").strip()

    ca_match = _CA_RE.search(cluster_body)
    ca_cert_path: str | None = None
    if ca_match:
        # We don't decode the base64; kubectl knows how to read it
        # via --certificate-authority-data. We just record its
        # presence for assertions.
        ca_cert_path = "<inline>"

    user_name, _ = users[0]

    ctx_name = _CURRENT_CTX_RE.search(text)
    if not ctx_name:
        # Fall back to the first context name.
        current_ctx = contexts[0][0]
        if not current_ctx:
            raise KubeconfigParseError(f"no current-context in {path}")
    else:
        current_ctx = ctx_name.group("v").strip()

    # Find the context block whose name matches current_ctx.
    matching_ctx: tuple[str, str] | None = None
    for c in contexts:
        if c[0] == current_ctx:
            matching_ctx = c
            break
    if matching_ctx is None:
        raise KubeconfigParseError(
            f"current-context '{current_ctx}' not found in `contexts:` of {path}"
        )
    _, ctx_body = matching_ctx

    cluster_ref = _CTX_CLUSTER_RE.search(ctx_body)
    user_ref = _CTX_USER_RE.search(ctx_body)
    ns_match = _CTX_NS_RE.search(ctx_body)
    default_namespace = ns_match.group("v").strip() if ns_match else "default"

    return Kubeconfig(
        path=path,
        api_endpoint=api_endpoint,
        cluster_name=cluster_ref.group("v").strip() if cluster_ref else cluster_name,
        user_name=user_ref.group("v").strip() if user_ref else user_name,
        context_name=current_ctx,
        default_namespace=default_namespace,
        ca_cert_path=ca_cert_path,
    )


__all__ = ["Kubeconfig", "KubeconfigParseError", "load"]
