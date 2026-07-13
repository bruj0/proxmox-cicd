"""container — DI wiring for the proxmox-cicd orchestrator.

Mirrors proxmox-k3s/provisioner/lib/container.py in shape:
the `Container` dataclass holds every concrete implementation
behind Protocols. Phases and apps receive a `Container` and
read `.helm`, `.kubectl`, `.kubeconfig`, `.logger`.

Two factories:

  Container.production(...)   — real subprocess runners.
  Container.for_tests(...)    — every runner is a MagicMock
                                injected by the test.

Filled out fully in WP6; the WP2 stub satisfies the type
checker so `cli.py` compiles end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .helm_runner import HelmRunner
from .kubectl_runner import KubectlRunner
from .log import StructuredLogger

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


@dataclass
class Container:
    """Dependency-injection container for the orchestrator."""

    repo_root: Path
    proxmox_k3s_repo: Path
    logger: StructuredLogger
    helm: HelmRunner = field(default_factory=HelmRunner)
    kubectl: KubectlRunner | None = None
    orchestrator: Orchestrator | None = None

    @classmethod
    def production(
        cls,
        proxmox_k3s_repo: Path,
        repo_root: Path,
    ) -> Container:
        """Construct a Container with real subprocess runners
        and the orchestrator pre-wired.
        """
        from datetime import UTC, datetime

        audit_log = (
            repo_root
            / "logs"
            / f"cicdctl_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.audit.jsonl"
        )
        logger = StructuredLogger(audit_path=audit_log)
        # Wire the logger into the helm runner so every helm
        # subprocess call lands in the same audit log file as the
        # orchestrator's high-level steps. Without this, the runner
        # silently captured stdout/stderr into CompletedProcess
        # fields that nothing read.
        helm = HelmRunner(logger=logger)
        container = cls(
            repo_root=repo_root,
            proxmox_k3s_repo=proxmox_k3s_repo,
            logger=logger,
            helm=helm,
        )
        # KubectlRunner is constructed lazily per-app (apps own
        # their own _kubectl() helper because they need the
        # cluster's kubeconfig path). Each app's helper now
        # passes logger=ctx.logger so kubectl calls land in the
        # same audit log.
        from .orchestrator import Orchestrator

        container.orchestrator = Orchestrator(container)
        return container

    @classmethod
    def for_tests(
        cls,
        proxmox_k3s_repo: Path,
        repo_root: Path,
        audit_log: Path | None = None,
    ) -> Container:
        """Construct a Container with audit logging to a tmp file."""
        from datetime import UTC, datetime

        if audit_log is None:
            audit_log = (
                repo_root
                / "logs"
                / f"test_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.audit.jsonl"
            )
        logger = StructuredLogger(audit_path=audit_log)
        helm = HelmRunner(logger=logger)
        container = cls(
            repo_root=repo_root,
            proxmox_k3s_repo=proxmox_k3s_repo,
            logger=logger,
            helm=helm,
        )
        from .orchestrator import Orchestrator

        container.orchestrator = Orchestrator(container)
        return container


__all__ = ["Container"]
