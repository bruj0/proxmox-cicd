"""helm_runner — thin wrapper around the `helm` CLI.

Idempotency is achieved by always using `helm upgrade
--install` rather than `helm install` / `helm upgrade`
separately. The runner never inspects release state itself;
helm reports the diff and we trust it.

We deliberately don't import the `helm` Python SDK. Same
rationale as `kubectl_runner`: the operator's audit log
should match exactly what they'd type at the terminal, and
the sibling repos all shell out to the system CLI.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# Helm install/upgrade timeouts. Install can take 2-3 minutes
# for a chart with CRDs (envoy-gateway, cert-manager) because
# the pre-upgrade hooks run a Job to maturity.
_DEFAULT_TIMEOUT_S = 180.0
_LONG_TIMEOUT_S = 600.0

# Cap on captured stdout/stderr that we write to the audit log.
# Subprocess.CompletedProcess already holds the full strings; we
# only truncate for the log line so the file doesn't explode when
# helm renders a large chart.
_LOG_TAIL_CHARS = 500

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

if TYPE_CHECKING:
    from .log import StructuredLogger


def _tail(text: str, limit: int = _LOG_TAIL_CHARS) -> str:
    """Return the last `limit` characters of `text`. Useful
    when surfacing helm's verbose stderr without dumping the
    entire output into the audit log.
    """
    if len(text) <= limit:
        return text
    return "...[truncated]..." + text[-limit:]


@dataclass
class HelmRunner:
    """Stateless wrapper around `helm upgrade --install`."""

    subprocess_runner: SubprocessRunner = subprocess.run
    env_base: dict[str, str] | None = None
    logger: StructuredLogger | None = None

    def _env(self) -> dict[str, str]:
        env = dict(self.env_base) if self.env_base else dict(os.environ)
        return env

    def _run(
        self,
        cmd: list[str],
        *,
        timeout_s: float,
        step: str,
    ) -> subprocess.CompletedProcess[str]:
        """Wrap subprocess.run with audit logging.

        Always emits one `info` line per call with cmd, rc,
        duration_s, stdout_tail, stderr_tail. On non-zero rc,
        emits an additional `warn` line so failures show up in
        the audit log without forcing the caller to log them.

        The audit log is the operator's only view of what
        actually happened on the cluster; without this wrapper
        the captured stdout/stderr was being silently dropped.
        """
        started = time.monotonic()
        result = self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )
        duration_s = round(time.monotonic() - started, 3)
        if self.logger is not None:
            self.logger.info(
                step=step,
                cmd=" ".join(cmd),
                rc=result.returncode,
                duration_s=duration_s,
                stdout_tail=_tail(result.stdout),
                stderr_tail=_tail(result.stderr),
            )
            if result.returncode != 0:
                self.logger.warn(
                    step=f"{step}_failed",
                    cmd=" ".join(cmd),
                    rc=result.returncode,
                    stderr_tail=_tail(result.stderr),
                )
        return result

    def _base_cmd(
        self,
        release: str,
        chart: str,
        namespace: str,
        version: str | None = None,
        values_files: tuple[Path, ...] = (),
        *,
        create_namespace: bool = True,
        extra_args: tuple[str, ...] = (),
    ) -> list[str]:
        cmd = [
            "helm",
            "upgrade",
            "--install",
            release,
            chart,
            "--namespace",
            namespace,
        ]
        if version:
            cmd.extend(["--version", version])
        if create_namespace:
            cmd.append("--create-namespace")
        for vf in values_files:
            cmd.extend(["-f", str(vf)])
        cmd.extend(["--wait", "--timeout", f"{int(_DEFAULT_TIMEOUT_S)}s"])
        cmd.extend(extra_args)
        return cmd

    def install_or_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_files: tuple[Path, ...] = (),
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        extra_args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        """`helm upgrade --install <release> <chart> ...`.

        `--wait` blocks until every resource is Ready. The
        default `extra_args=()` lets apps pass things like
        `--devel` for the bitwarden chart, which lives on a
        pre-release channel.
        """
        cmd = self._base_cmd(
            release,
            chart,
            namespace,
            version=version,
            values_files=values_files,
            extra_args=extra_args,
        )
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"helm.upgrade_install.{release}",
        )

    def uninstall(
        self,
        release: str,
        namespace: str,
        *,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`helm uninstall <release> -n <ns>` (idempotent)."""
        cmd = ["helm", "uninstall", release, "-n", namespace]
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"helm.uninstall.{release}",
        )

    def list_releases(
        self,
        namespace: str | None = None,
        *,
        all_namespaces: bool = False,
        timeout_s: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        """`helm list -n <ns>` (read-only)."""
        cmd = ["helm", "list"]
        if all_namespaces:
            cmd.append("-A")
        elif namespace:
            cmd.extend(["-n", namespace])
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step="helm.list",
        )

    def repo_add(
        self,
        name: str,
        url: str,
        *,
        timeout_s: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        """`helm repo add <name> <url>` (idempotent: --force-update)."""
        cmd = ["helm", "repo", "add", name, url, "--force-update"]
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"helm.repo_add.{name}",
        )

    def repo_update(self, timeout_s: float = 60.0) -> subprocess.CompletedProcess[str]:
        """`helm repo update` (refreshes the local chart cache)."""
        return self._run(
            ["helm", "repo", "update"],
            timeout_s=timeout_s,
            step="helm.repo_update",
        )

    def template(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_files: tuple[Path, ...] = (),
        timeout_s: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        """`helm template <release> <chart> -n <ns>`. Used in
        `validate` to render the chart without installing it.
        """
        cmd = [
            "helm",
            "template",
            release,
            chart,
            "--namespace",
            namespace,
        ]
        if version:
            cmd.extend(["--version", version])
        for vf in values_files:
            cmd.extend(["-f", str(vf)])
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"helm.template.{release}",
        )


__all__ = ["HelmRunner", "SubprocessRunner"]
