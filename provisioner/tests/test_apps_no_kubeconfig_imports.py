"""test_apps_no_kubeconfig_imports — WP6 static guard.

WP6 promise: after the WP6 refactor, no `apps/*.py`
file imports `proxmox_k3s` or pulls `Kubeconfig` /
`load` from `kubeconfig_loader`. The latter is the
direct test for the refactor; the former is the
forward-compat rule (the orchestrator and CLI
bootstrap-time only may still load the kubeconfig;
the apps must not).

This module is its own test file so a future
contributor running `pytest -k apps_no_kubeconfig`
gets a clean signal. Adding a new app that
inadvertently imports `proxmox_k3s` (e.g. to walk
the sibling repo's cluster directory) trips this
guard immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROVISIONER = Path(__file__).resolve().parents[2]  # repo root (proxmox-cicd/)
APPS_DIR = PROVISIONER / "provisioner" / "lib" / "apps"
FORBIDDEN_IMPORTS = (
    "from ..kubeconfig_loader",
    "from .kubeconfig_loader",
    "from provisioner.lib.kubeconfig_loader",
    "from ..kubectl_runner import Kubeconfig",
    "from ..kubectl_runner import KubeconfigRunner",  # keeps the rule restrictive
)


def _app_py_ids() -> list[str]:
    return [p.name for p in sorted(APPS_DIR.glob("*.py"))]


def _app_py_values() -> list[Path]:
    return sorted(APPS_DIR.glob("*.py"))


@pytest.mark.parametrize("app_py", _app_py_values(), ids=_app_py_ids())
def test_apps_no_kubeconfig_imports(app_py: Path) -> None:
    """No app module pulls `Kubeconfig` or
    `kubeconfig_loader` directly. Apps go through
    `BaseApp._kubectl(ctx)`, which holds the
    bootstrap logic.
    """
    src = app_py.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_IMPORTS:
        # The base.py file is the ONE place BaseApp
        # holds the implementation; everything else
        # must delegate. The exception is the helper
        # `types.py` import side-effects (none today).
        if forbidden not in src:
            continue
        if "from .base import" in src or app_py.name == "base.py":
            continue
        pytest.fail(
            f"{app_py.name} imports {forbidden!r}; "
            f"WP6 requires apps to delegate to "
            f"BaseApp._kubectl(ctx) instead."
        )


@pytest.mark.parametrize("app_py", _app_py_values(), ids=_app_py_ids())
def test_apps_no_lazy_kubeconfig_construction(app_py: Path) -> None:
    """No app module contains the
    `Kubeconfig.load(path)` lazy construction that
    WP6 hoists into BaseApp. Catches the failure mode
    where a contributor forgets to delete the per-app
    `_kubectl` after the refactor.
    """
    if app_py.name == "base.py":
        return
    src = app_py.read_text(encoding="utf-8")
    # The exact line we're trying to ban, plus two
    # close variants a future contributor might write.
    banned = (
        "Kubeconfig = load(path)",
        "kubeconfig: Kubeconfig = load(",
        "Kubeconfig.load(",
    )
    for needle in banned:
        if needle in src:
            pytest.fail(
                f"{app_py.name} still constructs "
                f"`Kubeconfig` directly ({needle!r}); "
                f"use BaseApp._kubectl(ctx) (WP6)."
            )
