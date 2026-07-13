"""WP2 tests — kubeconfig_loader, kubectl_runner, helm_runner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from provisioner.lib.helm_runner import HelmRunner
from provisioner.lib.kubeconfig_loader import (
    KubeconfigParseError,
    load,
)
from provisioner.lib.kubectl_runner import KubectlRunner, kubectl_on_path


# ----------------------------------------------------------- kubeconfig_loader


KUBECONFIG_YAML = """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ABCDEF==
    server: https://10.0.0.64:6443
  name: cicd
contexts:
- context:
    cluster: cicd
    user: cicd
    namespace: default
  name: cicd
current-context: cicd
users:
- name: cicd
  user:
    token: REDACTED-TOKEN-FOR-TESTS
"""


def test_kubeconfig_loader_parses_realistic_handoff(tmp_path: Path) -> None:
    p = tmp_path / "kubeconfig.yaml"
    p.write_text(KUBECONFIG_YAML)
    cfg = load(p)
    assert cfg.api_endpoint == "https://10.0.0.64:6443"
    assert cfg.cluster_name == "cicd"
    assert cfg.user_name == "cicd"
    assert cfg.context_name == "cicd"
    assert cfg.default_namespace == "default"
    assert cfg.ca_cert_path == "<inline>"
    assert cfg.path == p


def test_kubeconfig_loader_raises_when_file_missing(tmp_path: Path) -> None:
    with pytest.raises(KubeconfigParseError) as ei:
        load(tmp_path / "nope.yaml")
    assert "kubeconfig not found" in str(ei.value)
    assert "proxmox-k3s" in str(ei.value)


def test_kubeconfig_loader_raises_when_missing_section(tmp_path: Path) -> None:
    p = tmp_path / "kubeconfig.yaml"
    p.write_text("apiVersion: v1\nkind: Config\n")
    with pytest.raises(KubeconfigParseError):
        load(p)


def test_kubeconfig_loader_falls_back_to_first_context_when_no_current(
    tmp_path: Path,
) -> None:
    """No `current-context:` line — fallback to the first
    `contexts:` entry's name. This is the standard kubectl
    fallback (it's how `kubectl config use-context` writes).
    """
    p = tmp_path / "kubeconfig.yaml"
    p.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster:\n    server: https://x:6443\n  name: c\n"
        "users:\n- name: u\n  user:\n    token: t\n"
        "contexts:\n- context:\n    cluster: c\n    user: u\n  name: ctx\n"
    )
    cfg = load(p)
    assert cfg.context_name == "ctx"


# ----------------------------------------------------------- kubectl_runner


def _make_runner(tmp_path: Path) -> tuple[KubectlRunner, MagicMock]:
    p = tmp_path / "kubeconfig.yaml"
    p.write_text(KUBECONFIG_YAML)
    cfg = load(p)
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    return KubectlRunner(cfg, subprocess_runner=fake), fake


def test_kubectl_apply_passes_manifest_on_stdin(tmp_path: Path) -> None:
    runner, fake = _make_runner(tmp_path)
    manifest = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: gitea\n"
    runner.apply(manifest, namespace="gitea")
    assert fake.call_count == 1
    args, kwargs = fake.call_args
    cmd = args[0]
    assert cmd[0] == "kubectl"
    assert "--kubeconfig" in cmd
    assert "apply" in cmd
    assert "-f" in cmd
    assert "-" in cmd
    assert "-n" in cmd
    assert "gitea" in cmd
    assert "--server-side" in cmd
    assert kwargs["input"] == manifest
    assert kwargs["text"] is True


def test_kubectl_delete_supports_ignore_not_found(tmp_path: Path) -> None:
    runner, fake = _make_runner(tmp_path)
    runner.delete("namespace", "gitea", "gitea", ignore_not_found=True, wait=False)
    args, _ = fake.call_args
    assert "delete" in args[0]
    assert "--ignore-not-found" in args[0]
    assert "--wait=false" in args[0]


def test_kubectl_get_jsonpath(tmp_path: Path) -> None:
    runner, fake = _make_runner(tmp_path)
    fake.return_value.stdout = "10.0.0.64"
    runner.get(
        "nodes",
        "cicd-cp-1",
        jsonpath="{.status.addresses[0].address}",
    )
    args, _ = fake.call_args
    assert "jsonpath={.status.addresses[0].address}" in args[0]


def test_kubectl_wait_supports_condition(tmp_path: Path) -> None:
    runner, fake = _make_runner(tmp_path)
    runner.wait(
        "deploy/gitea", "gitea", "gitea", condition="condition=Available=true"
    )
    args, _ = fake.call_args
    assert "--for=condition=Available=true" in args[0]


def test_kubectl_on_path_is_a_thing() -> None:
    # Doesn't assert presence/absence — just that the
    # helper is callable and returns a bool.
    assert isinstance(kubectl_on_path(), bool)


# ----------------------------------------------------------- helm_runner


def test_helm_install_or_upgrade_builds_correct_command(tmp_path: Path) -> None:
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    runner = HelmRunner(subprocess_runner=fake)
    values = (tmp_path / "values.yaml",)
    runner.install_or_upgrade(
        release="gitea",
        chart="oci://docker.gitea.com/charts/gitea",
        namespace="gitea",
        version="12.0.0",
        values_files=values,
    )
    args, _ = fake.call_args
    cmd = args[0]
    assert cmd[0:3] == ["helm", "upgrade", "--install"]
    assert "gitea" in cmd
    assert "oci://docker.gitea.com/charts/gitea" in cmd
    assert "--version" in cmd
    assert "12.0.0" in cmd
    assert "--namespace" in cmd
    assert "gitea" in cmd
    assert "--create-namespace" in cmd
    assert "-f" in cmd
    assert str(values[0]) in cmd
    assert "--wait" in cmd


def test_helm_install_or_upgrade_passes_devel_extra_arg(tmp_path: Path) -> None:
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    runner = HelmRunner(subprocess_runner=fake)
    runner.install_or_upgrade(
        release="sm-operator",
        chart="bitwarden/sm-operator",
        namespace="sm-operator-system",
        extra_args=("--devel",),
    )
    args, _ = fake.call_args
    assert "--devel" in args[0]


def test_helm_uninstall_idempotent() -> None:
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    runner = HelmRunner(subprocess_runner=fake)
    runner.uninstall("gitea", "gitea")
    args, _ = fake.call_args
    assert args[0][0:3] == ["helm", "uninstall", "gitea"]


def test_helm_repo_add_uses_force_update() -> None:
    fake = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    runner = HelmRunner(subprocess_runner=fake)
    runner.repo_add("gitea-charts", "https://dl.gitea.com/charts/")
    args, _ = fake.call_args
    assert args[0][0:3] == ["helm", "repo", "add"]
    assert "--force-update" in args[0]


# ----------------------------------------------------------- subprocess timeout


def test_kubectl_apply_propagates_timeout(tmp_path: Path) -> None:
    """If subprocess times out, the runner surfaces the
    TimeoutExpired; we don't swallow it.
    """

    def raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=args[0] if args else [], timeout=30)

    runner, _ = _make_runner(tmp_path)
    runner.subprocess_runner = raise_timeout  # type: ignore[assignment]
    with pytest.raises(subprocess.TimeoutExpired):
        runner.apply("apiVersion: v1\nkind: Namespace\n", namespace="gitea")


# ----------------------------------------------------------- audit logging


def _make_logger(audit_path):
    """Inline-import StructuredLogger so the test file stays
    standalone if the existing tests above change their imports.
    """
    from provisioner.lib.log import StructuredLogger

    return StructuredLogger(audit_path=audit_path)


def _read_records(path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines()]


def test_helm_runner_emits_info_log_with_cmd_and_rc(tmp_path):
    audit = tmp_path / "audit.jsonl"
    logger = _make_logger(audit)
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="deployed\n", stderr="")
    )
    runner = HelmRunner(subprocess_runner=fake, logger=logger)

    result = runner.install_or_upgrade(
        release="sm-operator",
        chart="bitwarden/sm-operator",
        namespace="sm-operator-system",
        version="2.0.2",
    )

    assert result.returncode == 0
    records = _read_records(audit)
    assert len(records) == 1
    rec = records[0]
    assert rec["level"] == "info"
    assert rec["step"] == "helm.upgrade_install.sm-operator"
    assert "helm upgrade --install sm-operator bitwarden/sm-operator" in rec["data"]["cmd"]
    assert rec["data"]["rc"] == 0
    assert rec["data"]["stdout_tail"] == "deployed\n"
    assert rec["data"]["stderr_tail"] == ""
    assert rec["data"]["duration_s"] >= 0


def test_helm_runner_emits_warn_log_on_nonzero_rc(tmp_path):
    audit = tmp_path / "audit.jsonl"
    logger = _make_logger(audit)
    fake = MagicMock(
        return_value=MagicMock(
            returncode=1, stdout="", stderr="Error: chart not found\n"
        )
    )
    runner = HelmRunner(subprocess_runner=fake, logger=logger)

    result = runner.install_or_upgrade(
        release="bad",
        chart="bitwarden/sm-operator",
        namespace="sm-operator-system",
        version="0.0.0",
    )

    assert result.returncode == 1
    records = _read_records(audit)
    # 1 info + 1 warn
    assert len(records) == 2
    assert records[0]["level"] == "info"
    assert records[0]["step"] == "helm.upgrade_install.bad"
    assert records[1]["level"] == "warn"
    assert records[1]["step"] == "helm.upgrade_install.bad_failed"
    assert records[1]["data"]["rc"] == 1
    assert "chart not found" in records[1]["data"]["stderr_tail"]


def test_helm_runner_silent_when_logger_is_none():
    """Backward-compat: HelmRunner() with no logger should not
    blow up. (Tests in this suite do exactly that.)"""
    fake = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    runner = HelmRunner(subprocess_runner=fake)
    # No audit_path; no logger; must still return CompletedProcess.
    result = runner.install_or_upgrade(
        release="x", chart="y/z", namespace="ns",
    )
    assert result.returncode == 0
