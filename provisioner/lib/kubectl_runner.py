"""kubectl_runner — thin wrapper around the `kubectl` CLI.

Every method shells out to `kubectl --kubeconfig <path> ...`.
The runner is stateful only about the kubeconfig path; every
call is otherwise a fresh subprocess (kubectl doesn't have a
useful persistent connection model that beats `kubectl apply`
over a unix socket).

We don't import the `kubernetes` Python client because:
  1. It's a heavyweight runtime dep we don't need.
  2. We want the runner's audit log to match exactly what the
     operator would type at the terminal.
  3. The sibling repos (proxmox-vms, proxmox-k3s) already
     shell out to the system CLI and we want consistency.

For tests, every method takes optional `subprocess_runner` /
`env` overrides so they can be mocked without monkey-patching
`subprocess.run` globally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .kubeconfig_loader import Kubeconfig

# Default timeout for a single kubectl call. Set high enough for
# slow apiservers but low enough that a hung `kubectl wait` does
# not stall the orchestrator indefinitely.
_DEFAULT_TIMEOUT_S = 60.0
_LONG_TIMEOUT_S = 300.0

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class KubectlRunner:
    """Stateless wrapper around `kubectl --kubeconfig <path>`."""

    kubeconfig: Kubeconfig
    subprocess_runner: SubprocessRunner = subprocess.run
    env_base: dict[str, str] | None = None

    def _base_cmd(self, *args: str) -> list[str]:
        return [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig.path),
            *args,
        ]

    def _env(self) -> dict[str, str]:
        env = dict(self.env_base) if self.env_base else dict(os.environ)
        env["KUBECONFIG"] = str(self.kubeconfig.path)
        return env

    # ------------------------------------------------------ writes

    def apply(
        self,
        manifest: str,
        namespace: str | None = None,
        *,
        server_side: bool = True,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl apply -n <ns> -f -` (stdin = manifest).

        Server-side apply is the default because (a) it
        preserves fields the chart's admission webhook writes
        even when the manifest doesn't include them, and
        (b) it makes diff deterministic across orchestrator
        re-runs.
        """
        cmd = self._base_cmd("apply", "-f", "-")
        if namespace:
            cmd.extend(["-n", namespace])
        if server_side:
            cmd.append("--server-side")
        return self.subprocess_runner(  # noqa: S603
            cmd,
            input=manifest,
            text=True,
            check=False,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )

    def delete(
        self,
        resource: str,
        name: str,
        namespace: str,
        *,
        ignore_not_found: bool = True,
        wait: bool = True,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl delete <resource> <name> -n <ns>`."""
        cmd = self._base_cmd("delete", resource, name, "-n", namespace)
        if ignore_not_found:
            cmd.append("--ignore-not-found")
        cmd.append(f"--wait={'true' if wait else 'false'}")
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )

    def delete_namespace(
        self,
        namespace: str,
        *,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl delete ns <ns> --wait=true`."""
        cmd = self._base_cmd("delete", "ns", namespace, "--wait=true")
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )

    # ------------------------------------------------------ reads

    def get(
        self,
        resource: str,
        name: str | None = None,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        jsonpath: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl get ... -o jsonpath=...` (read-only)."""
        cmd = self._base_cmd("get", resource)
        if name:
            cmd.append(name)
        if namespace:
            cmd.extend(["-n", namespace])
        if label_selector:
            cmd.extend(["-l", label_selector])
        if jsonpath:
            cmd.extend(["-o", f"jsonpath={jsonpath}"])
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )

    def wait(
        self,
        resource: str,
        name: str,
        namespace: str,
        *,
        condition: str,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl wait --for=condition=<cond> ...`."""
        cmd = self._base_cmd(
            "wait",
            f"--for={condition}",
            f"--timeout={int(timeout_s)}s",
            f"{resource}/{name}",
            "-n",
            namespace,
        )
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s + 30.0,
            capture_output=True,
            env=self._env(),
        )

    def wait_deployments_available(
        self,
        namespace: str,
        label_selector: str,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """Convenience: wait for every Deployment matching the
        label selector in the namespace to become Available.
        """
        cmd = self._base_cmd(
            "wait",
            "deploy",
            "-l",
            label_selector,
            "-n",
            namespace,
            "--for=condition=Available=true",
            f"--timeout={int(timeout_s)}s",
        )
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s + 30.0,
            capture_output=True,
            env=self._env(),
        )

    def version(self, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
        """`kubectl version --client --output=yaml`.

        Used by `validate` to assert that kubectl is installed
        at all. The orchestrator's preflight calls this before
        anything else.
        """
        cmd = self._base_cmd("version", "--client=true", "--output=yaml")
        return self.subprocess_runner(  # noqa: S603
            cmd,
            check=False,
            text=True,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )


def kubectl_on_path() -> bool:
    """Pre-flight check: is `kubectl` on PATH?"""
    return shutil.which("kubectl") is not None


def helm_on_path() -> bool:
    """Pre-flight check: is `helm` on PATH?"""
    return shutil.which("helm") is not None


def kubeconfig_path_for(proxmox_k3s_repo: Path, cluster: str) -> Path:
    """Compute the canonical kubeconfig path for a cluster."""
    return proxmox_k3s_repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"


__all__ = [
    "KubectlRunner",
    "SubprocessRunner",
    "helm_on_path",
    "kubectl_on_path",
    "kubeconfig_path_for",
]


_ = Any  # keep Any referenced for the dataclass TypeVar-like patterns
