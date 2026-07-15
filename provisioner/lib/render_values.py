"""render_values — WP10 deep-merge + write helper.

WP10 ships a single-purpose helper that deep-merges a
shipped defaults dict with a per-cluster overlay
dict and writes the result to a stable path:

    <repo_root>/.proxmox-cicd/rendered/<cluster>/<app>.yaml

The orchestrator catches the helper before any
apply/destroy logic runs — see `cicdctl render` in
`cli.py`. The render is a **read-only inspection tool**
for operators: it produces the YAML the orchestrator
*would* generate on apply, without applying anything.

The deep-merge logic lives in `apps/base.py`
(`BaseApp._deep_merge`, shipped with WP9). This module
is a thin orchestrator over that helper + a writer.

The render cache lives under `.proxmox-cicd/`, which
is the codebase's gitignored scratch area (it's
already in `.gitignore` for the rendered values
themselves). The orchestrator, the CLI, and
`BaseApp._rendered_values_file` all agree on the path
formula:

    repo_root / ".proxmox-cicd" / "rendered" / cluster / f"{app}.yaml"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .apps.base import BaseApp


class NoShippedDefaultsError(RuntimeError):
    """The shipped `default_values` for this app is
    empty AND no cluster overlay was provided.

    WP10 — raises a clear error so the operator can
    grep the audit log. The orchestrator surfaces this
    as `EXIT_CATALOG` (pre-apply) — the app simply has
    nothing to render.
    """


def render_for_app(
    app_name: str,
    cluster_name: str,
    repo_root: Path,
    shipped_defaults: dict[str, Any],
    cluster_overlay: dict[str, Any] | None,
) -> Path:
    """Deep-merge `shipped_defaults` + `cluster_overlay`
    for one app; write the result to
    `<repo_root>/.proxmox-cicd/rendered/<cluster>/<app>.yaml`;
    return the rendered path.

    The merge rule (mirrors WP1 §5.2 and WP9's
    `BaseApp._deep_merge`):

      * shipped dict is the base
      * per-cluster overlay wins on a per-key basis
      * nested dicts merge recursively so a partial
        override at `path: foo` doesn't clobber the
        rest of the shipped `path` block

    If both sides are empty (`shipped_defaults` is
    `{}` AND `cluster_overlay` is `None` or `{}`),
    raise `NoShippedDefaultsError` so the operator
    knows the render produced nothing.

    The shipped defaults dict MUST NOT be mutated
    (`BaseApp._deep_merge` is pure-function); a future
    change that breaks this contract trips the
    `test_render_uses_shipped_defaults_when_no_cluster_file`
    case which round-trips the input.
    """
    if not shipped_defaults and not cluster_overlay:
        raise NoShippedDefaultsError(
            f"app {app_name!r} has no shipped defaults and "
            f"no per-cluster overlay; nothing to render. "
            f"Add `default_values:` to shipped.yaml or a "
            f"per-cluster overlay."
        )

    base: dict[str, Any] = dict(shipped_defaults)
    if cluster_overlay:
        base = BaseApp._deep_merge(base, cluster_overlay)

    out_path = render_path(repo_root, cluster_name, app_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(base, sort_keys=False),
        encoding="utf-8",
    )
    return out_path


def render_path(repo_root: Path, cluster_name: str, app_name: str) -> Path:
    """The canonical render-cache path for
    `<app>` under `<cluster>`.

    Pure path computation — no I/O, no parent
    creation. Apps reach for this when they want to
    inspect the path without rendering.
    """
    return repo_root / ".proxmox-cicd" / "rendered" / cluster_name / f"{app_name}.yaml"


__all__ = [
    "NoShippedDefaultsError",
    "render_for_app",
    "render_path",
]
