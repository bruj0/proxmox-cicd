#!/usr/bin/env python3
"""Post-renderer: strip helm-emitted labels and annotations
from one specific Secret in the rendered chart output, so
VaultwardenK8sSync can own it without a field-manager
conflict.

Used by the cloudflared app (`helm upgrade --install
cloudflare-tunnel-remote --post-renderer ...`) to drop
labels from `Secret/cloudflare-tunnel-remote` only —
Deployment/ServiceAccount/etc. are passed through
unchanged so helm continues to own them.

Why a post-renderer instead of a chart fork
-------------------------------------------

Forks vendoring carry the maintenance burden of every
chart bump. The upstream chart is already minimal — the
only fix needed is "this Secret should not be helm-owned".
A post-renderer is a single-file overlay that intercepts
the rendered YAML just before `kubectl apply` and rewrites
the targeted document. No chart changes, no `helm
dependency update` cycles, no `Chart.yaml` rewrites.

Wire-up (in `provisioner/lib/apps/cloudflared.py`):

    from ..helm_post_renderers.strip_helm_secret_labels import (
        SCRIPT_PATH,
    )

    result = ctx.helm.install_or_upgrade(
        ...,
        extra_args=("--post-renderer", str(SCRIPT_PATH)),
    )

Why a label strip rather than a label merge
-------------------------------------------

When VKS writes the Secret, it sets:

    labels:
      app.kubernetes.io/managed-by: vaultwarden-kubernetes-secrets
      app.kubernetes.io/created-by:  vaultwarden-k8s-sync
      app.kubernetes.io/instance:    cloudflare-tunnel-remote
      app.kubernetes.io/name:        cloudflare-tunnel-remote
      app.kubernetes.io/version:     latest

When helm writes it, it sets the same five
`app.kubernetes.io/*` keys plus `helm.sh/chart`. The
content is *almost* identical except for the `managed-by`
value (`Helm` vs `vaultwarden-kubernetes-secrets`) and the
chart field.

A merge would race. A strip gives helm exactly one job:
"create or update this Secret's `data` block; do not
assert any labels". VKS handles everything else.

Input/output contract
---------------------

- stdin: YAML stream, multiple `---`-separated documents.
- stdout: YAML stream, same number of documents, same
  schema. The targeted Secret document has its
  `metadata.labels` and `metadata.annotations` rewritten
  to *only* contain non-helm keys (i.e. anything VKS may
  have written). All other documents pass through
  byte-identical.

CLI entry point
---------------

`./strip_helm_secret_labels.py` (or `python -m provisioner.
lib.helm_post_renderers.strip_helm_secret_labels`) runs the
post-renderer against stdin/stdout for live helm
integration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

# Default target: the cloudflared chart's only Secret.
# Override via CLI flags when the orchestrator wants to
# reuse this script for other charts.
DEFAULT_TARGET_KIND = "Secret"
DEFAULT_TARGET_NAME = "cloudflare-tunnel-remote"

# Helm labels and annotations the chart emits via
# `_helpers.tpl::labels` and the implicit
# `meta.helm.sh/release-*` set helm applies on every
# resource. We strip all of these from the targeted
# Secret so kubectl apply treats the Secret as
# "no owner of these fields". Other label keys (e.g.
# `app.kubernetes.io/part-of: ...` that VKS or some
# downstream controller might add later) pass through.
HELM_LABEL_KEYS = frozenset(
    {
        "helm.sh/chart",
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/version",
        "app.kubernetes.io/managed-by",
        "app.kubernetes.io/created-by",
    }
)
HELM_ANNOTATION_KEYS = frozenset(
    {
        "meta.helm.sh/release-name",
        "meta.helm.sh/release-namespace",
    }
)

# Where this script lives on disk. Used by the
# orchestrator to pass `--post-renderer <path>` to helm.
SCRIPT_PATH = Path(__file__).resolve()


def strip_helm_labels(
    doc: dict[str, Any],
    *,
    target_kind: str = DEFAULT_TARGET_KIND,
    target_name: str = DEFAULT_TARGET_NAME,
) -> dict[str, Any]:
    """Strip helm-emitted labels/annotations from one
    specific document, return the document (mutated in
    place for efficiency, also returned for chaining).

    The match is `kind == target_kind AND metadata.name
    == target_name`. Any other document passes through
    unchanged. The matching document has its
    `metadata.labels` reduced to the set of keys NOT in
    `HELM_LABEL_KEYS`, and its `metadata.annotations`
    reduced to the set of keys NOT in
    `HELM_ANNOTATION_KEYS`. `metadata.labels` and
    `metadata.annotations` themselves are kept (possibly
    as empty dicts) so the YAML structure round-trips
    cleanly through `yaml.safe_dump`.
    """
    if not isinstance(doc, dict):
        return doc
    if doc.get("kind") != target_kind:
        return doc
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return doc
    if metadata.get("name") != target_name:
        return doc

    labels = metadata.get("labels")
    if isinstance(labels, dict):
        metadata["labels"] = {
            k: v for k, v in labels.items() if k not in HELM_LABEL_KEYS
        }

    annotations = metadata.get("annotations")
    if isinstance(annotations, dict):
        metadata["annotations"] = {
            k: v for k, v in annotations.items() if k not in HELM_ANNOTATION_KEYS
        }

    return doc


def render(stream: str, *, target_kind: str, target_name: str) -> str:
    """Read a YAML stream, strip helm labels from the
    targeted document, write the YAML stream back.

    Helm's post-renderer contract is "read YAML on stdin,
    write YAML on stdout". Documents are preserved in
    order; the targeted document is the only one mutated.
    Empty documents (helm emits a trailing `---` after
    `NOTES.txt` sometimes) are dropped silently. The
    output uses `yaml.safe_dump_all` so multi-document
    streams re-emit `---` separators, which both helm
    and kubectl handle transparently.
    """
    docs: list[Any] = []
    for doc in yaml.safe_load_all(stream):
        if doc is None:
            continue
        strip_helm_labels(doc, target_kind=target_kind, target_name=target_name)
        docs.append(doc)
    return yaml.safe_dump_all(
        docs,
        sort_keys=False,
        default_flow_style=False,
        explicit_start=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Post-renderer for helm: strip helm-emitted "
            "labels/annotations from a single targeted "
            "Secret so VaultwardenK8sSync can own it "
            "without a kubectl field-manager conflict."
        ),
    )
    parser.add_argument(
        "--target-kind",
        default=DEFAULT_TARGET_KIND,
        help=f"kind to strip (default: {DEFAULT_TARGET_KIND})",
    )
    parser.add_argument(
        "--target-name",
        default=DEFAULT_TARGET_NAME,
        help=f"name to strip (default: {DEFAULT_TARGET_NAME})",
    )
    args = parser.parse_args(argv)
    sys.stdout.write(render(sys.stdin.read(), target_kind=args.target_kind, target_name=args.target_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
