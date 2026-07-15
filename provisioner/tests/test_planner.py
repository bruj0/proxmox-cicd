"""test_planner — WP7 output-shape contract.

WP7's deliverable is "plan output mentions the group".
Most of WP7 was already shipped with WP3 (the
`group` parameter on `build_plan()` and `PlanDiff.render()`
was added then). This test file pins the WP7 *output
shape* so a future refactor of the planner doesn't
silently drop the group header or the error-row
rendering.

The tests cover:

  * `PlanDiff.render()` emits the group header
    (`Group: <name>`) at the top, after the cluster
    name banner.
  * `PlanDiff.render()` surfaces errors in a
    dedicated `ERRORS:` block before any app rows.
  * `PlanDiff.render()` with no apps selected emits
    `(no apps selected)` after the group header.
  * `PlanDiff.render()` includes the per-app
    install/apply/note lines emitted by each
    `AppSpec.plan()`.
"""

from __future__ import annotations

from pathlib import Path

from provisioner.lib.apps import AppPlanResult
from provisioner.lib.planner import PlanDiff


def test_plan_render_emits_cluster_and_group_headers() -> None:
    """The first two lines of any plan output are the
    cluster name banner and the `Group:` header. The
    order matters — operators read top-to-bottom."""
    diff = PlanDiff(cluster_name="cicd")
    out = diff.render(group="cicd-stack")
    lines = out.splitlines()
    assert lines[0] == "Plan for cluster 'cicd':"
    assert lines[1] == "  Group: cicd-stack"


def test_plan_render_default_group_when_unspecified() -> None:
    """`render()` with no `group` argument falls back
    to `default`. WP7 — `cicdctl plan cicd` without
    `--group` uses the default group; the operator
    sees it spelled out in the output."""
    diff = PlanDiff(cluster_name="cicd")
    out = diff.render()
    assert "  Group: default" in out


def test_plan_render_surfaces_errors_before_app_rows() -> None:
    """Errors get a dedicated `ERRORS:` block placed
    after the cluster/group header and *before* any
    app rows. The orchestrator catches catalog
    errors during `build_plan()`; operators reading
    the plan need to see them up top."""
    diff = PlanDiff(
        cluster_name="cicd",
        errors=["unknown app 'foo' in catalog"],
    )
    out = diff.render(group="cicd-stack")
    lines = out.splitlines()
    # Header (2) + blank (1) + '  ERRORS:' (1) + bullet (1)
    assert lines[0] == "Plan for cluster 'cicd':"
    assert lines[1] == "  Group: cicd-stack"
    # ERRORS block — no app rows after it.
    errors_idx = lines.index("  ERRORS:")
    assert lines[errors_idx + 1] == "    - unknown app 'foo' in catalog"
    assert "+ app:" not in out
    assert "install:" not in out


def test_plan_render_empty_when_no_apps_selected() -> None:
    """A cluster with no enabled apps gets the
    `(no apps selected)` line under the group header.
    The render returns a stable string the CLI can
    pipe to `less` without surprises."""
    diff = PlanDiff(cluster_name="staging")
    out = diff.render(group="default")
    lines = out.splitlines()
    assert lines[0] == "Plan for cluster 'staging':"
    assert lines[1] == "  Group: default"
    assert lines[2] == "  (no apps selected)"


def test_plan_render_lists_each_app_row() -> None:
    """The render walks `rows` in order, emitting
    `+ app: <name>` followed by per-row install/apply/
    note lines. The shape is the public contract
    operators pipe to `less`."""
    rows = [
        AppPlanResult(
            app_name="gitea",
            would_install=["helm upgrade --install gitea ..."],
            would_apply=["kubectl apply Secret=gitea-admin-password"],
            notes=["renders the rendered values file"],
        ),
        AppPlanResult(
            app_name="cloudflared",
            would_install=["helm upgrade --install cloudflare-tunnel-remote ..."],
            would_apply=[],
            notes=["no cloudflared-specific apply"],
        ),
    ]
    diff = PlanDiff(cluster_name="cicd", rows=rows)
    out = diff.render(group="cicd-stack")
    # Gitea row
    assert "  + app: gitea" in out
    assert "      install: helm upgrade --install gitea ..." in out
    assert "      apply:   kubectl apply Secret=gitea-admin-password" in out
    assert "      note:    renders the rendered values file" in out
    # Cloudflared row
    assert "  + app: cloudflared" in out
    assert "      install: helm upgrade --install cloudflare-tunnel-remote ..." in out
    assert "      note:    no cloudflared-specific apply" in out


def test_plan_render_lists_skipped_after_header() -> None:
    """Apps registered but not enabled in the catalog
    show up in `skipped`. The block is rendered after
    the cluster/group header, before app rows."""
    diff = PlanDiff(
        cluster_name="cicd",
        skipped=["gitea-runner"],
    )
    out = diff.render(group="cicd-stack")
    lines = out.splitlines()
    assert lines[0] == "Plan for cluster 'cicd':"
    assert lines[1] == "  Group: cicd-stack"
    # Skipped block has its own line with the full list.
    assert any(
        "skipped (not enabled in catalog)" in line
        and "gitea-runner" in line
        for line in lines
    )


def test_plan_render_path_for_piped_output(tmp_path: Path) -> None:
    """The render returns a string that ends with a
    newline so the CLI can pipe it to `less` or
    `tee` without missing the last line. WP7 didn't
    change this contract — but pinning it here
    catches a future contributor who swaps the
    `"\n".join(...) + "\n"` for a different idiom."""
    diff = PlanDiff(cluster_name="cicd")
    out = diff.render(group="cicd-stack")
    assert out.endswith("\n")
    # Should be valid for `out.write_text(...)` round-trip.
    target = tmp_path / "plan.txt"
    target.write_text(out)
    assert target.read_text() == out
