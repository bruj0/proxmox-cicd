"""vaultwarden-k8s-sync app — installs VaultwardenK8sSync (VKS).

Source: https://github.com/antoniolago/vaultwarden-kubernetes-secrets
Chart:   oci://ghcr.io/antoniolago/charts/vaultwarden-kubernetes-secrets

VKS is the successor to the bitwarden-sm-operator chart we used
to install: where bitwarden-sm-operator watched BitwardenSecret
CRs and reconciled them into k8s Secrets, VKS polls a Vaultwarden
(or Bitwarden-compatible) server directly and writes k8s Secrets
from items tagged with the configured namespace field. The
operator config (server URL, organization, collection, etc.) lives
in values/vaultwarden-kubernetes-secrets.yaml; the chart itself
is upstream-published (chart 2.0.0, matches appVersion 2.0.0).

Install contract:

  helm upgrade --install vaultwarden-kubernetes-secrets \
    oci://ghcr.io/antoniolago/charts/vaultwarden-kubernetes-secrets \
    --version 2.0.0 \
    -n vaultwarden-kubernetes-secrets \
    --create-namespace \
    -f values/vaultwarden-kubernetes-secrets.yaml

What this app does on apply:
  1. helm install (chart 2.0.0) using the OCI URL above.
  2. Wait for the sync Deployment to be Available.
  3. Print the operator next-step: BW_CLIENTID / BW_CLIENTSECRET
     / VAULTWARDEN__MASTERPASSWORD must be seeded into the
     auth Secret before the sync starts polling. The chart's
     auth-secret.yaml auto-creates the Secret with a random
     API token but leaves the BW_* fields empty.

Idempotency: helm upgrade --install + kubectl wait. Re-runs are
no-op for the chart; the post-apply Secret seed step is also
no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..container import Container
from . import AppApplyResult, AppPlanResult, AppStatus, register

# Chart constants. Pinned to chart 2.0.0 (matches appVersion 2.0.0
# — the local clone at /home/bruj0/projects/vaultwarden-kubernetes-secrets/
# has Chart.yaml version 0.1.0 + appVersion "latest", but that's a
# dev tag; the published chart that the operator should consume is
# 2.0.0 from the OCI registry).
REPO_NAME = "vaultwarden-kubernetes-secrets"
CHART = "oci://ghcr.io/antoniolago/charts/vaultwarden-kubernetes-secrets"
CHART_VERSION = "2.0.0"  # pinned in versions.yaml
APP_VERSION = "2.0.0"    # matches Chart.yaml appVersion
NAMESPACE = "vaultwarden-kubernetes-secrets"
RELEASE = "vaultwarden-kubernetes-secrets"
DEFAULT_VALUES_FILE = "values/vaultwarden-kubernetes-secrets.yaml"


@dataclass
class VaultwardenK8sSyncApp:
    """AppSpec for VaultwardenK8sSync (VKS)."""

    name: str = "vaultwarden-k8s-sync"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / DEFAULT_VALUES_FILE

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm upgrade --install {RELEASE} {CHART} "
                f"--version {CHART_VERSION} -n {NAMESPACE} "
                f"--create-namespace -f "
                f"{self._values_file(ctx)}",
            ],
            would_apply=[],
            notes=[
                f"chart: {CHART}@{CHART_VERSION}",
                f"image: ghcr.io/antoniolago/"
                f"vaultwarden-kubernetes-secrets:{APP_VERSION}",
                "operator image: ghcr.io/antoniolago/"
                "vaultwarden-kubernetes-secrets (sync service)",
                (
                    "registers CRD: (none — VKS is a polling "
                    "service, not a CRD controller)"
                ),
                (
                    "post-install: seed BW_CLIENTID / "
                    "BW_CLIENTSECRET / VAULTWARDEN__MASTERPASSWORD "
                    "into the auth Secret. The chart's "
                    "auth-secret.yaml auto-creates the Secret "
                    "shell but leaves the BW_* keys empty."
                ),
            ],
        )

    def apply(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppApplyResult:
        values = self._values_file(ctx)
        if not values.exists():
            raise FileNotFoundError(
                f"vaultwarden-k8s-sync values file not found: "
                f"{values}. Run from proxmox-cicd root."
            )

        ctx.logger.info(
            "vaultwarden_k8s_sync.helm_install_started",
            release=RELEASE,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
            chart=CHART,
        )

        # 1. helm upgrade --install. The OCI chart carries its
        #    own --wait semantics; we let it default.
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
            "vaultwarden_k8s_sync.helm_install_ok",
            release=RELEASE,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
        )

        # 2. Wait for the sync Deployment to be Available.
        kubectl = self._kubectl(ctx)
        wait = kubectl.wait_deployments_available(
            namespace=NAMESPACE,
            label_selector="app.kubernetes.io/name=vaultwarden-kubernetes-secrets",
            timeout_s=120.0,
        )
        if wait.returncode != 0:
            # Don't raise — the pod may still be crashing
            # because the BW_* Secret is empty. Surface the
            # failure as a warning so the operator sees it in
            # the audit log + the post-apply next-step.
            ctx.logger.warn(
                "vaultwarden_k8s_sync.deployments_not_available",
                stderr=wait.stderr.strip()[:500],
                resolution=(
                    "the BW_CLIENTID/BW_CLIENTSECRET/VAULTWARDEN__MASTERPASSWORD "
                    "fields in the auth Secret are likely empty. "
                    "See the apply.next_step log line for the "
                    "seed command."
                ),
            )

        # 3. Surface the operator-visible next step. The sync
        #    service starts polling only after the BW_*
        #    credentials land in the auth Secret. The chart's
        #    auth-secret.yaml auto-creates the Secret shell
        #    with a random API token; the BW_* fields must
        #    be added by the operator.
        ctx.logger.info(
            "vaultwarden_k8s_sync.waiting_for_credentials",
            secret=f"{NAMESPACE}/vaultwarden-kubernetes-secrets",
            next_step=(
                "seed the auth Secret with Vaultwarden "
                "credentials. From your Vaultwarden account: "
                "Settings -> Account -> API Key, copy the "
                "client_id + client_secret. Then: "
                "kubectl -n vaultwarden-kubernetes-secrets "
                "create secret generic vaultwarden-kubernetes-secrets "
                "--from-literal=BW_CLIENTID=<uuid> "
                "--from-literal=BW_CLIENTSECRET=<secret> "
                "--from-literal=VAULTWARDEN__MASTERPASSWORD=<password> "
                "--dry-run=client -o yaml | kubectl apply -f -. "
                "The sync service will start polling within "
                "~30s once the Secret is updated."
            ),
        )

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=RELEASE,
            chart_version=CHART_VERSION,
            image_version=APP_VERSION,
            ingress_host=None,
            next_step=(
                f"open https://bitwarden.bruj0.net and create "
                f"an API key, then seed the auth Secret in "
                f"namespace {NAMESPACE} (key BW_CLIENTID, "
                "BW_CLIENTSECRET, VAULTWARDEN__MASTERPASSWORD). "
                "The sync service starts polling within ~30s "
                "of the Secret update."
            ),
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        kubectl = self._kubectl(ctx)
        # Uninstall the helm release first (this deletes the
        # Deployment + Service + RBAC + ConfigMap).
        helm_result = ctx.helm.uninstall(RELEASE, NAMESPACE, timeout_s=120.0)
        if helm_result.returncode != 0:
            ctx.logger.warn(
                "vaultwarden_k8s_sync.helm_uninstall_failed",
                release=RELEASE,
                stderr=helm_result.stderr.strip()[:500],
            )
        # Then delete the namespace (which deletes the auth
        # Secret + ConfigMap).
        del_result = kubectl.delete_namespace(NAMESPACE, timeout_s=120.0)
        if del_result.returncode != 0:
            ctx.logger.warn(
                "vaultwarden_k8s_sync.namespace_delete_failed",
                namespace=NAMESPACE,
                stderr=del_result.stderr.strip()[:500],
            )
        ctx.logger.info(
            "vaultwarden_k8s_sync.destroyed", namespace=NAMESPACE
        )

    def status(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppStatus:
        list_result = ctx.helm.list_releases(namespace=NAMESPACE, timeout_s=15.0)
        release_present = (
            list_result.returncode == 0 and RELEASE in list_result.stdout
        )
        chart_version: str | None = CHART_VERSION if release_present else None
        image_version: str | None = APP_VERSION if release_present else None

        notes: list[str] = []
        if not release_present:
            notes.append(
                "release not installed; run `cicdctl apply cicd`"
            )
        else:
            notes.append(
                "the sync Deployment is up. Check that the "
                "auth Secret (vaultwarden-kubernetes-secrets) "
                "has BW_CLIENTID/BW_CLIENTSECRET/VAULTWARDEN__"
                "MASTERPASSWORD populated, otherwise the sync "
                "service will keep restarting with auth errors."
            )

        return AppStatus(
            app_name=self.name,
            namespace=NAMESPACE,
            release_present=release_present,
            chart_version=chart_version,
            image_version=image_version,
            ingress_host=None,
            notes=notes,
        )

    def _kubectl(self, ctx: Container) -> Any:
        # Late import to avoid a circular dep at module load.
        from ..kubectl_runner import KubectlRunner

        if ctx.kubectl is not None:
            return ctx.kubectl
        from ..kubeconfig_loader import Kubeconfig, load

        cluster = "cicd"
        path = ctx.proxmox_k3s_repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"
        kubeconfig: Kubeconfig = load(path)
        kubectl = KubectlRunner(kubeconfig=kubeconfig, logger=ctx.logger)
        ctx.kubectl = kubectl
        return kubectl


__all__ = [
    "VaultwardenK8sSyncApp",
    "CHART",
    "CHART_VERSION",
    "APP_VERSION",
    "NAMESPACE",
    "RELEASE",
]


register(VaultwardenK8sSyncApp)
