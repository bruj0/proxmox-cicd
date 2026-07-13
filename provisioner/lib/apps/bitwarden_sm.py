"""bitwarden-sm-operator app — installs the Bitwarden Secrets Manager controller.

Source: https://bitwarden.com/help/secrets-manager-kubernetes-operator/

The Bitwarden SM operator is a small controller that watches
`BitwardenSecret` CRDs and syncs secrets from Bitwarden
Secrets Manager into Kubernetes Secrets. It's a pure
in-cluster control plane — no persistence, no ingress.

Once the controller is installed, every other app in the
catalog can reference BitwardenSecret CRs to source
secrets (the gitea-runner app uses this to pull the
runner registration token).

Install command (verbatim from the Bitwarden docs):

  helm repo add bitwarden https://charts.bitwarden.com/
  helm repo update
  helm upgrade sm-operator bitwarden/sm-operator -i \
    --debug -n sm-operator-system --create-namespace \
    --values my-values.yaml

Pins:
  - chart: bitwarden/sm-operator:2.0.2
  - operator image: bitwarden/sm-operator:2.1.0 (from chart appVersion)

Idempotency: helm upgrade --install + kubectl wait.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..container import Container
from ..kubectl_runner import KubectlRunner
from . import AppApplyResult, AppPlanResult, AppStatus, register

# Chart constants.
REPO_NAME = "bitwarden"
REPO_URL = "https://charts.bitwarden.com/"
CHART = "bitwarden/sm-operator"
CHART_VERSION = "2.0.2"  # pinned in versions.yaml
OPERATOR_IMAGE_VERSION = "2.1.0"
NAMESPACE = "sm-operator-system"
RELEASE = "sm-operator"
DEFAULT_VALUES_FILE = "values/bitwarden-sm-operator.yaml"


@dataclass
class BitwardenSmApp:
    """AppSpec for the Bitwarden SM operator."""

    name: str = "bitwarden-sm-operator"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / DEFAULT_VALUES_FILE

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm repo add {REPO_NAME} {REPO_URL}",
                f"helm upgrade --install {RELEASE} {CHART} "
                f"--version {CHART_VERSION} -n {NAMESPACE} "
                f"--create-namespace -f "
                f"{self._values_file(ctx)}",
            ],
            would_apply=[],
            notes=[
                f"chart: {CHART}@{CHART_VERSION}",
                f"operator image: bitwarden/sm-operator:"
                f"{OPERATOR_IMAGE_VERSION}",
                "registers CRD: bitwardensecrets.k8s.bitwarden.com",
                (
                    "optional: bw-access-token Secret in "
                    f"{NAMESPACE} with key 'token' for "
                    "BitwardenSecret CRs to authenticate"
                ),
            ],
        )

    def apply(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppApplyResult:
        values = self._values_file(ctx)
        if not values.exists():
            raise FileNotFoundError(
                f"bitwarden-sm-operator values file not found: "
                f"{values}. Run `make apply` from proxmox-cicd root."
            )

        # 1. helm repo add bitwarden. Idempotent via --force-update.
        repo_add = ctx.helm.repo_add(REPO_NAME, REPO_URL, timeout_s=30.0)
        if repo_add.returncode != 0:
            ctx.logger.warn(
                "bitwarden_sm.repo_add_failed",
                name=REPO_NAME,
                stderr=repo_add.stderr.strip()[:500],
            )
        # helm repo update so we get the pinned chart version.
        repo_update = ctx.helm.repo_update(timeout_s=60.0)
        if repo_update.returncode != 0:
            ctx.logger.warn(
                "bitwarden_sm.repo_update_failed",
                stderr=repo_update.stderr.strip()[:500],
            )

        # 2. helm upgrade --install (stable channel; no --devel).
        result = ctx.helm.install_or_upgrade(
            release=RELEASE,
            chart=CHART,
            namespace=NAMESPACE,
            version=CHART_VERSION,
            values_files=(values,),
            timeout_s=300.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"helm install {RELEASE} failed: "
                f"rc={result.returncode} stderr={result.stderr.strip()[:500]}"
            )
        ctx.logger.info(
            "bitwarden_sm.helm_install_ok",
            release=RELEASE,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
        )

        # 3. Wait for the operator Deployment to be Available.
        kubectl = self._kubectl(ctx)
        wait = kubectl.wait_deployments_available(
            namespace=NAMESPACE,
            label_selector="app.kubernetes.io/name=sm-operator",
            timeout_s=180.0,
        )
        if wait.returncode != 0:
            ctx.logger.warn(
                "bitwarden_sm.deployments_not_available",
                stderr=wait.stderr.strip()[:500],
            )

        # 4. Verify the BitwardenSecret CRD is registered.
        crd_check = kubectl.get(
            "crd",
            "bitwardensecrets.k8s.bitwarden.com",
            jsonpath="{.metadata.name}",
            timeout_s=15.0,
        )
        crd_present = crd_check.returncode == 0 and bool(
            crd_check.stdout.strip()
        )
        if not crd_present:
            ctx.logger.warn(
                "bitwarden_sm.crd_not_registered",
                message=(
                    "BitwardenSecret CRD not registered; the "
                    "operator may still be initialising. "
                    "Other apps depending on BitwardenSecret "
                    "CRs (e.g. gitea-runner) will fail until "
                    "the CRD appears."
                ),
            )

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=RELEASE,
            chart_version=CHART_VERSION,
            image_version=OPERATOR_IMAGE_VERSION,
            ingress_host=None,
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        kubectl = self._kubectl(ctx)
        helm_result = ctx.helm.uninstall(RELEASE, NAMESPACE, timeout_s=120.0)
        if helm_result.returncode != 0:
            ctx.logger.warn(
                "bitwarden_sm.helm_uninstall_failed",
                release=RELEASE,
                stderr=helm_result.stderr.strip()[:500],
            )
        del_result = kubectl.delete_namespace(NAMESPACE, timeout_s=120.0)
        if del_result.returncode != 0:
            ctx.logger.warn(
                "bitwarden_sm.namespace_delete_failed",
                namespace=NAMESPACE,
                stderr=del_result.stderr.strip()[:500],
            )
        ctx.logger.info("bitwarden_sm.destroyed", namespace=NAMESPACE)

    def status(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppStatus:
        list_result = ctx.helm.list_releases(namespace=NAMESPACE, timeout_s=15.0)
        release_present = (
            list_result.returncode == 0 and RELEASE in list_result.stdout
        )
        notes: list[str] = []
        if not release_present:
            notes.append("release not installed; run `cicdctl apply cicd`")
            # Don't probe the cluster if the release isn't there.
            return AppStatus(
                app_name=self.name,
                namespace=NAMESPACE,
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
                notes=notes,
            )

        # Release IS present — probe the CRD to detect a stuck install.
        kubectl = self._kubectl(ctx)
        crd_check = kubectl.get(
            "crd",
            "bitwardensecrets.k8s.bitwarden.com",
            jsonpath="{.metadata.name}",
            timeout_s=10.0,
        )
        crd_present = crd_check.returncode == 0 and bool(
            crd_check.stdout.strip()
        )
        if not crd_present:
            notes.append(
                "operator running but BitwardenSecret CRD not "
                "registered (controller still initialising?)"
            )

        return AppStatus(
            app_name=self.name,
            namespace=NAMESPACE,
            release_present=True,
            chart_version=CHART_VERSION,
            image_version=OPERATOR_IMAGE_VERSION,
            ingress_host=None,
            notes=notes,
        )

    def _kubectl(self, ctx: Container) -> KubectlRunner:
        if ctx.kubectl is not None:
            return ctx.kubectl
        from ..kubeconfig_loader import Kubeconfig, load

        cluster = os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")
        path = ctx.proxmox_k3s_repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"
        kubeconfig: Kubeconfig = load(path)
        kubectl = KubectlRunner(kubeconfig=kubeconfig, logger=ctx.logger)
        ctx.kubectl = kubectl
        return kubectl


# Side-effect import: register on import.
register(BitwardenSmApp)


__all__ = [
    "BitwardenSmApp",
    "CHART",
    "CHART_VERSION",
    "NAMESPACE",
    "OPERATOR_IMAGE_VERSION",
    "RELEASE",
]
