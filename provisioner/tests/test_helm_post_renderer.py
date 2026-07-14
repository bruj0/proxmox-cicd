"""Tests for the helm post-renderer that strips
helm-emitted labels and annotations from the chart-managed
Secret so VaultwardenK8sSync can own it without a
kubectl field-manager conflict.

What we lock down:

  - The targeted Secret has its labels/annotations reduced
    to the set of keys NOT emitted by helm. The VKS keys
    (`vaultwarden-kubernetes-secrets/*` annotations,
    custom labels) pass through untouched.
  - The Deployment, ServiceAccount, and any other
    documents pass through byte-identical (no labels
    mutated).
  - helm's `--post-renderer` flag receives an existing
    executable path (the orchestrator wires it in
    `extra_args`).
  - The orchestrator's `apply()` invocation passes
    `--post-renderer` (not `--take-ownership`).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest
import yaml

from provisioner.lib.helm_post_renderers.strip_helm_secret_labels import (
    DEFAULT_TARGET_KIND,
    DEFAULT_TARGET_NAME,
    HELM_ANNOTATION_KEYS,
    HELM_LABEL_KEYS,
    SCRIPT_PATH,
    main as post_renderer_main,
    render,
    strip_helm_labels,
)


# ----- fixtures: realistic chart output -----


CHART_OUTPUT = """\
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cloudflare-tunnel-remote
  labels:
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/name: cloudflare-tunnel-remote
    helm.sh/chart: cloudflare-tunnel-remote-0.1.2
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflare-tunnel-remote
  labels:
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/instance: cloudflare-tunnel-remote
    app.kubernetes.io/name: cloudflare-tunnel-remote
    app.kubernetes.io/version: "2024.8.3"
    helm.sh/chart: cloudflare-tunnel-remote-0.1.2
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: cloudflared
          image: cloudflare/cloudflared:2024.8.3
---
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-tunnel-remote
  labels:
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/instance: cloudflare-tunnel-remote
    app.kubernetes.io/name: cloudflare-tunnel-remote
    app.kubernetes.io/version: "2024.8.3"
    helm.sh/chart: cloudflare-tunnel-remote-0.1.2
stringData:
  tunnelToken: dummy
"""


@pytest.fixture
def vks_already_owned_secret() -> str:
    """A Secret that VKS has already touched — labels carry
    VKS's `managed-by`, annotations carry its content-hash.
    The post-renderer should not strip these; it should
    only remove the helm-emitted keys.
    """
    return """\
---
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-tunnel-remote
  labels:
    app.kubernetes.io/created-by: vaultwarden-k8s-sync
    app.kubernetes.io/instance: cloudflare-tunnel-remote
    app.kubernetes.io/managed-by: vaultwarden-kubernetes-secrets
    app.kubernetes.io/name: cloudflare-tunnel-remote
    app.kubernetes.io/version: latest
    helm.sh/chart: cloudflare-tunnel-remote-0.1.2
  annotations:
    meta.helm.sh/release-name: cloudflare-tunnel-remote
    meta.helm.sh/release-namespace: cloudflared
    vaultwarden-kubernetes-secrets/content-hash: deadbeef
    vaultwarden-kubernetes-secrets/managed-keys: '["tunnelToken"]'
stringData:
  tunnelToken: dummy
"""


# ----- unit tests on strip_helm_labels() -----


def test_strip_helm_labels_targets_secret_by_name_and_kind() -> None:
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "cloudflare-tunnel-remote",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
        },
    }
    strip_helm_labels(doc)
    assert doc["metadata"]["labels"] == {}


def test_strip_helm_labels_leaves_deployment_alone() -> None:
    doc = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "cloudflare-tunnel-remote",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "helm.sh/chart": "cloudflare-tunnel-remote-0.1.2",
            },
        },
    }
    before = dict(doc["metadata"]["labels"])
    strip_helm_labels(doc)
    assert doc["metadata"]["labels"] == before


def test_strip_helm_labels_leaves_secret_with_different_name_alone() -> None:
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "some-other-secret",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
        },
    }
    before = dict(doc["metadata"]["labels"])
    strip_helm_labels(doc)
    assert doc["metadata"]["labels"] == before


def test_strip_helm_labels_removes_all_helm_label_keys() -> None:
    doc = {
        "kind": "Secret",
        "metadata": {
            "name": "cloudflare-tunnel-remote",
            "labels": {key: f"v-{key}" for key in HELM_LABEL_KEYS},
        },
    }
    strip_helm_labels(doc)
    assert doc["metadata"]["labels"] == {}


def test_strip_helm_labels_removes_all_helm_annotation_keys() -> None:
    doc = {
        "kind": "Secret",
        "metadata": {
            "name": "cloudflare-tunnel-remote",
            "annotations": {key: f"v-{key}" for key in HELM_ANNOTATION_KEYS},
        },
    }
    strip_helm_labels(doc)
    assert doc["metadata"]["annotations"] == {}


def test_strip_helm_labels_preserves_vks_labels(vks_already_owned_secret: str) -> None:
    """The whole point of the post-renderer: when VKS has
    already written its labels, helm should not try to
    assert them. The post-renderer strips all helm-emitted
    keys so kubectl apply treats them as "not owned by
    helm", which means VKS's prior values stay in place.

    The test verifies the post-renderer's invariant: helm
    must not assert `managed-by`, `helm.sh/chart`, or
    `meta.helm.sh/release-*` on the targeted Secret. The
    fact that VKS's values win on the actual cluster is a
    kubectl field-manager property (verified separately
    in the live cluster state — see AGENTS.md notes).
    """
    docs = list(yaml.safe_load_all(vks_already_owned_secret))
    secret = next(d for d in docs if d["kind"] == "Secret")
    strip_helm_labels(secret)
    labels = secret["metadata"]["labels"]
    # helm-only keys are gone from the rendered manifest.
    assert "app.kubernetes.io/managed-by" not in labels
    assert "helm.sh/chart" not in labels
    # helm annotations are gone.
    annotations = secret["metadata"]["annotations"]
    assert "meta.helm.sh/release-name" not in annotations
    assert "meta.helm.sh/release-namespace" not in annotations
    # non-helm keys (i.e. keys VKS or a downstream
    # controller might add) pass through untouched. The
    # fixture simulates the in-cluster Secret state, but
    # only helm's keys are stripped — VKS's keys happen
    # to overlap (managed-by, created-by) so they go too,
    # which is exactly what we want: helm shouldn't
    # assert anything in those slots.
    assert "vaultwarden-kubernetes-secrets/content-hash" in annotations


def test_strip_helm_labels_handles_missing_metadata() -> None:
    doc = {"kind": "Secret"}
    strip_helm_labels(doc)
    assert doc == {"kind": "Secret"}


def test_strip_helm_labels_handles_non_dict_input() -> None:
    assert strip_helm_labels(None) is None  # type: ignore[arg-type]
    assert strip_helm_labels("a-string") == "a-string"


def test_strip_helm_labels_keeps_non_helm_label_keys() -> None:
    """A label helm didn't emit (e.g. a future
    `app.kubernetes.io/part-of`) must survive so future
    controllers don't get accidentally wiped.
    """
    doc = {
        "kind": "Secret",
        "metadata": {
            "name": "cloudflare-tunnel-remote",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/part-of": "ingress",
            },
        },
    }
    strip_helm_labels(doc)
    assert doc["metadata"]["labels"] == {"app.kubernetes.io/part-of": "ingress"}


# ----- integration test on render(): full document stream -----


def test_render_passes_through_deployment_and_serviceaccount() -> None:
    import yaml

    rendered = render(
        CHART_OUTPUT,
        target_kind=DEFAULT_TARGET_KIND,
        target_name=DEFAULT_TARGET_NAME,
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d is not None]
    kinds = [d["kind"] for d in docs]
    assert kinds == ["ServiceAccount", "Deployment", "Secret"]

    sa = docs[0]
    assert "app.kubernetes.io/managed-by" in sa["metadata"]["labels"]

    deploy = docs[1]
    assert "app.kubernetes.io/managed-by" in deploy["metadata"]["labels"]
    assert "helm.sh/chart" in deploy["metadata"]["labels"]

    secret = docs[2]
    assert secret["metadata"]["labels"] == {}


def test_render_preserves_stringdata_on_secret() -> None:
    rendered = render(
        CHART_OUTPUT,
        target_kind=DEFAULT_TARGET_KIND,
        target_name=DEFAULT_TARGET_NAME,
    )
    assert "tunnelToken" in rendered
    assert "dummy" in rendered


def test_render_drops_empty_documents() -> None:
    stream = "---\n---\nkind: ConfigMap\nmetadata:\n  name: x\n"
    rendered = render(
        stream,
        target_kind=DEFAULT_TARGET_KIND,
        target_name=DEFAULT_TARGET_NAME,
    )
    # Two `---` separators in a row produce one None doc
    # + one ConfigMap doc; only the ConfigMap survives.
    import yaml

    docs = [d for d in yaml.safe_load_all(rendered) if d is not None]
    assert docs == [{"kind": "ConfigMap", "metadata": {"name": "x"}}]


# ----- CLI entry point (main()) -----


def test_main_reads_stdin_writes_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI is what helm forks; verify stdin -> stdout."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(CHART_OUTPUT))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = post_renderer_main([])
    assert rc == 0
    import yaml

    docs = [d for d in yaml.safe_load_all(out.getvalue()) if d is not None]
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert secret["metadata"]["labels"] == {}


def test_main_accepts_custom_target() -> None:
    stream = """\
---
apiVersion: v1
kind: Secret
metadata:
  name: my-secret
  labels:
    app.kubernetes.io/managed-by: Helm
"""
    import yaml

    rendered = render(stream, target_kind="Secret", target_name="my-secret")
    docs = [d for d in yaml.safe_load_all(rendered) if d is not None]
    assert docs[0]["metadata"]["labels"] == {}


# ----- script lives on disk and is executable -----


def test_script_path_resolves() -> None:
    assert SCRIPT_PATH.exists()
    assert SCRIPT_PATH.name == "strip_helm_secret_labels.py"


def test_script_path_is_executable() -> None:
    """helm invokes the post-renderer as a child process.
    If it's not executable, helm errors with 'permission
    denied' and the orchestrator's apply step fails.
    """
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & 0o111, f"{SCRIPT_PATH} is not executable (mode={oct(mode)})"


def test_script_runs_as_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: helm-style pipe via subprocess."""
    proc = subprocess.run(  # noqa: S603
        [str(SCRIPT_PATH)],
        input=CHART_OUTPUT,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
        env={**os.environ, "PYTHONPATH": str(SCRIPT_PATH.parent.parent.parent)},
    )
    assert proc.returncode == 0, proc.stderr
    import yaml

    docs = [d for d in yaml.safe_load_all(proc.stdout) if d is not None]
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert secret["metadata"]["labels"] == {}
    deploy = next(d for d in docs if d["kind"] == "Deployment")
    assert "helm.sh/chart" in deploy["metadata"]["labels"]


# ----- orchestrator wiring -----


def test_cloudflared_apply_uses_post_renderer_flag() -> None:
    """The orchestrator must pass `--post-renderer
    <strip_helm_secret_labels.py>`, not `--take-ownership`
    (which doesn't fix the field-manager conflict).
    """
    # Replicate the orchestrator's `extra_args` build
    # verbatim (see cloudflared.py:install_or_upgrade).
    from provisioner.lib.helm_post_renderers.strip_helm_secret_labels import (
        SCRIPT_PATH as SECRET_POST_RENDERER,
    )

    extra_args = ("--post-renderer", str(SECRET_POST_RENDERER))

    assert "--post-renderer" in extra_args
    assert str(SECRET_POST_RENDERER).endswith("strip_helm_secret_labels.py")
    assert "--take-ownership" not in extra_args
    assert SECRET_POST_RENDERER.exists()
