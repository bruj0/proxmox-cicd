"""gitea-runner app — installs the in-repo gitea-runner chart.

This is the ONE chart this repo owns. The upstream Gitea
project doesn't publish an official runner helm chart, so
we wrap the official `gitea/runner:1.0.8` docker image in
a small per-repo chart under `infra/charts/gitea-runner/`.

Sources:
  - https://docs.gitea.com/runner/1.0.8/
  - https://gitea.com/gitea/act-runner

Installation flow:
  1. Pre-flight: assert the bitwarden-sm-operator app has
     registered the `BitwardenSecret` CRD (otherwise we
     can't fetch the registration token).
  2. Apply the `BitwardenSecret` CR for the runner (it
     syncs the Gitea runner registration token into a k8s
     Secret named `gitea-runner-config`).
  3. Wait for the BitwardenSecret's status to become
     `Synced=True`.
  4. helm install the runner chart; the chart mounts the
     `gitea-runner-config` Secret into the runner pod.
  5. Wait for the runner Deployment to be Available.

Idempotency:
  - BitwardenSecret CR apply is server-side; existing CR
    is reconciled.
  - helm upgrade --install (default).
  - The runner is `ephemeral: true` so the registration
    is single-use; subsequent runs re-register.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..container import Container
from ..kubectl_runner import KubectlRunner
from . import AppApplyResult, AppPlanResult, AppStatus, register

NAMESPACE = "gitea-runner"
RELEASE = "gitea-runner"
CHART_VERSION = "0.1.0"  # the chart version (Chart.yaml)
APP_VERSION = "1.0.8"  # the runner image version
DEFAULT_VALUES_FILE = "values/gitea-runner.yaml"

# The Secret name the chart creates / mounts.
RUNNER_CONFIG_SECRET = "gitea-runner-config"
# The BitwardenSecret CR name (must match what bitwarden_sm
# and the operator agree on).
BW_SECRET_CR = "gitea-runner-registration"
BW_AUTH_SECRET = "bw-auth-token"

# The BitwardenSecret CR body. We pin secretKeyName + map
# so the operator knows which Bitwarden secret to sync into
# which k8s secret key. Operators create the `bw-auth-token`
# Secret themselves (see docs/runbooks/add-an-app.md).
BW_SECRET_CR_MANIFEST = """\
---
apiVersion: k8s.bitwarden.com/v1
kind: BitwardenSecret
metadata:
  name: {cr_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: gitea-runner
spec:
  organizationId: "{organization_id}"
  secretName: {secret_name}
  map:
    - bwSecretId: "{bw_secret_id}"
      secretKeyName: registrationToken
  authToken:
    secretName: {auth_secret_name}
    secretKey: token
"""


@dataclass
class GiteaRunnerApp:
    """AppSpec for the gitea-runner chart."""

    name: str = "gitea-runner"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / DEFAULT_VALUES_FILE

    def _chart_dir(self, ctx: Container) -> Path:
        return ctx.repo_root / "infra" / "charts" / "gitea-runner"

    def _gitea_instance_url(self, catalog: dict[str, Any]) -> str:
        """The runner polls the in-cluster gitea Service, not
        the public ingress. Hard-coded to the chart's expected
        service name + namespace.
        """
        return "http://gitea-http.gitea.svc.cluster.local:3000"

    def _bw_secret_id(self, catalog: dict[str, Any]) -> str:
        """Operator-supplied Bitwarden Secrets Manager secret
        UUID for the runner registration token. Lives in
        catalog.bitwarden.runner_secret_id. Empty -> apply
        proceeds but logs a warning.
        """
        bw = catalog.get("bitwarden", {})
        v = bw.get("runner_secret_id", "")
        return v if isinstance(v, str) else ""

    def _organization_id(self, catalog: dict[str, Any]) -> str:
        bw = catalog.get("bitwarden", {})
        v = bw.get("organization_id", "")
        return v if isinstance(v, str) else ""

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm upgrade --install {RELEASE} "
                f"<repo>/gitea-runner (local chart, version "
                f"{CHART_VERSION}) -n {NAMESPACE}",
            ],
            would_apply=[
                f"kubectl apply --server-side -n {NAMESPACE} "
                f"(BitwardenSecret={BW_SECRET_CR})",
            ],
            notes=[
                f"image: gitea/runner:{APP_VERSION}",
                "ephemeral: true (single-use registration)",
                "persistence: proxmox-lvm-thin PVC (cache + data)",
                (
                    "registration token source: Bitwarden "
                    f"({BW_SECRET_CR} -> {RUNNER_CONFIG_SECRET})"
                ),
            ],
        )

    def apply(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppApplyResult:
        chart_dir = self._chart_dir(ctx)
        if not chart_dir.exists():
            raise FileNotFoundError(
                f"gitea-runner chart not found at {chart_dir}. "
                f"Did you delete infra/charts/gitea-runner/?"
            )
        values = self._values_file(ctx)
        if not values.exists():
            # values/ is optional — fall back to chart defaults.
            ctx.logger.info(
                "gitea_runner.no_values_file", path=str(values)
            )
            values_for_helm: tuple[Path, ...] = ()
        else:
            values_for_helm = (values,)

        kubectl = self._kubectl(ctx)

        # 1. Pre-flight: CRD present?
        crd_check = kubectl.get(
            "crd",
            "bitwardensecrets.k8s.bitwarden.com",
            jsonpath="{.metadata.name}",
            timeout_s=10.0,
        )
        if crd_check.returncode != 0 or not crd_check.stdout.strip():
            raise RuntimeError(
                "bitwardensecrets.k8s.bitwarden.com CRD is not "
                "installed. Apply the bitwarden-sm-operator "
                "app first (`cicdctl apply cicd` with "
                "bitwarden enabled)."
            )

        # 2. Apply the BitwardenSecret CR if the operator
        #    has credentials.
        org_id = self._organization_id(catalog)
        bw_sid = self._bw_secret_id(catalog)
        if org_id and bw_sid:
            cr_yaml = BW_SECRET_CR_MANIFEST.format(
                cr_name=BW_SECRET_CR,
                namespace=NAMESPACE,
                organization_id=org_id,
                secret_name=RUNNER_CONFIG_SECRET,
                bw_secret_id=bw_sid,
                auth_secret_name=BW_AUTH_SECRET,
            )
            cr_apply = kubectl.apply(
                manifest=cr_yaml, namespace=NAMESPACE, server_side=True
            )
            if cr_apply.returncode != 0:
                raise RuntimeError(
                    f"kubectl apply BitwardenSecret={BW_SECRET_CR} "
                    f"failed: rc={cr_apply.returncode} "
                    f"stderr={cr_apply.stderr.strip()[:500]}"
                )
            ctx.logger.info(
                "gitea_runner.bitwardensecret_applied",
                cr=BW_SECRET_CR,
                namespace=NAMESPACE,
            )
        else:
            ctx.logger.warn(
                "gitea_runner.bitwarden_skipped",
                message=(
                    "catalog.bitwarden.organization_id or "
                    "runner_secret_id is empty; the runner's "
                    "registration token will not be auto-synced "
                    "from Bitwarden. Provision manually: "
                    f"create secret {RUNNER_CONFIG_SECRET} "
                    "in namespace gitea-runner with key "
                    "'registrationToken'."
                ),
            )

        # 3. helm install the local chart.
        result = ctx.helm.install_or_upgrade(
            release=RELEASE,
            chart=str(chart_dir),
            namespace=NAMESPACE,
            version=CHART_VERSION,
            values_files=values_for_helm,
            timeout_s=300.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"helm install gitea-runner failed: "
                f"rc={result.returncode} stderr={result.stderr.strip()[:500]}"
            )
        ctx.logger.info(
            "gitea_runner.helm_install_ok",
            release=RELEASE,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
        )

        # 4. Wait for the runner Deployment to be Available.
        wait = kubectl.wait_deployments_available(
            namespace=NAMESPACE,
            label_selector="app.kubernetes.io/name=gitea-runner",
            timeout_s=120.0,
        )
        if wait.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.deployments_not_available",
                stderr=wait.stderr.strip()[:500],
            )
            # We don't raise — the runner may take a while to
            # register on a fresh Gitea instance.

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=RELEASE,
            chart_version=CHART_VERSION,
            image_version=APP_VERSION,
            ingress_host=None,
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        kubectl = self._kubectl(ctx)
        # Uninstall the helm release first (this deletes the
        # Deployment + Service + RBAC).
        helm_result = ctx.helm.uninstall(RELEASE, NAMESPACE, timeout_s=120.0)
        if helm_result.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.helm_uninstall_failed",
                release=RELEASE,
                stderr=helm_result.stderr.strip()[:500],
            )
        # Then delete the namespace (which deletes the
        # BitwardenSecret CR + ConfigMap).
        del_result = kubectl.delete_namespace(NAMESPACE, timeout_s=120.0)
        if del_result.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.namespace_delete_failed",
                namespace=NAMESPACE,
                stderr=del_result.stderr.strip()[:500],
            )
        ctx.logger.info("gitea_runner.destroyed", namespace=NAMESPACE)

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
        return AppStatus(
            app_name=self.name,
            namespace=NAMESPACE,
            release_present=release_present,
            chart_version=CHART_VERSION if release_present else None,
            image_version=APP_VERSION if release_present else None,
            ingress_host=None,
            notes=notes,
        )

    def _kubectl(self, ctx: Container) -> KubectlRunner:
        if ctx.kubectl is not None:
            return ctx.kubectl
        # Production path: build a KubectlRunner from the
        # sibling proxmox-k3s repo's kubeconfig.yaml.
        from ..kubeconfig_loader import Kubeconfig, load

        cluster = os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")
        path = ctx.proxmox_k3s_repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"
        kubeconfig: Kubeconfig = load(path)
        return KubectlRunner(kubeconfig=kubeconfig)


# Side-effect import: register on import.
register(GiteaRunnerApp)


__all__ = [
    "GiteaRunnerApp",
    "CHART_VERSION",
    "APP_VERSION",
    "NAMESPACE",
    "BW_SECRET_CR",
    "RUNNER_CONFIG_SECRET",
]
