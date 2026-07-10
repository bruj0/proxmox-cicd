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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Helm install/upgrade timeouts. Install can take 2-3 minutes
# for a chart with CRDs (envoy-gateway, cert-manager) because
# the pre-upgrade hooks run a Job to maturity.
_DEFAULT_TIMEOUT_S = 180.0
_LONG_TIMEOUT_S = 600.0

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class HelmRunner:
    """Stateless wrapper around `helm upgrade --install`."""

    subprocess_runner: SubprocessRunner = subprocess.run
    env_base: dict[str, str] | None = None

    def _env(self) -> dict[str, str]:
        env = dict(self.env_base) if self.env_base else dict(os.environ)
        return env

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
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
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
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
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
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
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
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )

    def repo_update(self, timeout_s: float = 60.0) -> subprocess.CompletedProcess[str]:
        """`helm repo update` (refreshes the local chart cache)."""
        return self.subprocess_runner(  # noqa: S603
            ["helm", "repo", "update"],
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
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
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )


__all__ = ["HelmRunner", "SubprocessRunner"]
