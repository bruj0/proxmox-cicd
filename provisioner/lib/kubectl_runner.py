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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .kubeconfig_loader import Kubeconfig

# Default timeout for a single kubectl call. Set high enough for
# slow apiservers but low enough that a hung `kubectl wait` does
# not stall the orchestrator indefinitely.
_DEFAULT_TIMEOUT_S = 60.0
_LONG_TIMEOUT_S = 300.0

# Cap on captured stdout/stderr that we write to the audit log.
_LOG_TAIL_CHARS = 500

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

if TYPE_CHECKING:
    from .log import StructuredLogger


def _tail(text: str, limit: int = _LOG_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "...[truncated]..." + text[-limit:]


@dataclass
class KubectlRunner:
    """Stateless wrapper around `kubectl --kubeconfig <path>`."""

    kubeconfig: Kubeconfig
    subprocess_runner: SubprocessRunner = subprocess.run
    env_base: dict[str, str] | None = None
    logger: StructuredLogger | None = None

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

    def _run(
        self,
        cmd: list[str],
        *,
        timeout_s: float,
        step: str,
    ) -> subprocess.CompletedProcess[str]:
        """Wrap subprocess.run with audit logging (mirrors HelmRunner._run).

        Every kubectl call emits one `info` line per invocation
        with cmd, rc, duration_s, stdout_tail, stderr_tail; a
        non-zero rc also emits a `warn` line.
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
        # Apply takes manifest on stdin, so use subprocess_runner
        # directly with the input kwarg (the _run helper would
        # discard the `input` arg).
        started = time.monotonic()
        result = self.subprocess_runner(  # noqa: S603
            cmd,
            input=manifest,
            text=True,
            check=False,
            timeout=timeout_s,
            capture_output=True,
            env=self._env(),
        )
        duration_s = round(time.monotonic() - started, 3)
        if self.logger is not None:
            self.logger.info(
                step=f"kubectl.apply.{namespace or 'default'}",
                cmd=" ".join(cmd),
                rc=result.returncode,
                duration_s=duration_s,
                manifest_bytes=len(manifest),
                stderr_tail=_tail(result.stderr),
            )
            if result.returncode != 0:
                self.logger.warn(
                    step="kubectl.apply_failed",
                    cmd=" ".join(cmd),
                    rc=result.returncode,
                    stderr_tail=_tail(result.stderr),
                )
        return result

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
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"kubectl.delete.{resource}.{name}",
        )

    def delete_namespace(
        self,
        namespace: str,
        *,
        timeout_s: float = _LONG_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl delete ns <ns> --wait=true`."""
        cmd = self._base_cmd("delete", "ns", namespace, "--wait=true")
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"kubectl.delete_ns.{namespace}",
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
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"kubectl.get.{resource}",
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
        return self._run(
            cmd,
            timeout_s=timeout_s + 30.0,
            step=f"kubectl.wait.{resource}.{name}",
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
        return self._run(
            cmd,
            timeout_s=timeout_s + 30.0,
            step=f"kubectl.wait_deploys.{namespace}",
        )

    def version(self, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
        """`kubectl version --client --output=yaml`.

        Used by `validate` to assert that kubectl is installed
        at all. The orchestrator's preflight calls this before
        anything else.
        """
        cmd = self._base_cmd("version", "--client=true", "--output=yaml")
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step="kubectl.version",
        )

    def run_oneshot(
        self,
        *,
        image: str,
        namespace: str,
        command: list[str],
        timeout_s: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl run <name> --rm -i --restart=Never --image=<image> -n <ns> -- <cmd>`.

        Spawns a one-shot pod, runs the command, captures the
        output, and the pod auto-deletes (`--rm`). Used by the
        status smoke tests to probe in-cluster Service URLs
        without needing a port-forward.

        Two kubectl quirks this helper navigates:

          * `kubectl run` requires a positional `NAME`
            (the pod name); it MUST satisfy RFC 1123
            (lowercase alphanumeric + `-`). Auto-generated
            pod names use `<NAME>-<rand>` so the suffix is
            always lowercase.
          * Without `--command`, the first positional arg
            after `--` becomes the pod's `containers[0].name`
            and Kubernetes rejects `/bin/sh` ("lowercase RFC
            1123 label must consist of ..."). With
            `--command --`, the first positional is the
            container's `command:` and the rest are its
            `args:` — no RFC 1123 collision.
        """
        cmd = self._base_cmd(
            "run",
            "cicd-smoke",  # pod name; satisfies RFC 1123
            "--rm",
            "-i",
            "--quiet=true",
            "--restart=Never",
            f"--image={image}",
            f"--namespace={namespace}",
            "--command",
            "--",
            *command,
        )
        return self._run(
            cmd,
            timeout_s=timeout_s,
            step=f"kubectl.run_oneshot.{namespace}",
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
