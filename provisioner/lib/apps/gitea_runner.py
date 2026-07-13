"""gitea-runner app — installs the in-repo gitea-runner chart.

This is the ONE chart this repo owns. The upstream Gitea
project doesn't publish an official runner helm chart, so
we wrap the official `gitea/runner:1.0.8` docker image in
a small per-repo chart under `infra/charts/gitea-runner/`.

Sources:
  - https://docs.gitea.com/runner/1.0.8/
  - https://gitea.com/gitea/act-runner

Installation flow:
  1. helm install the runner chart. The chart's
     `secret.yaml` template creates the
     `gitea-runner-config` Secret shell with a
     placeholder `registrationToken` value on first
     install. The chart's deployment.yaml mounts the
     Secret as a volume at `/etc/runner/token` — the
     `registrationToken` key becomes the file content.
  2. Wait for the runner Deployment to be Available.
     The runner pod goes into CrashLoopBackOff until
     the Secret has a non-empty registrationToken;
     this is expected and the post-apply next-step
     walks the operator through the population path.
  3. The apply does NOT seed the Secret with a
     placeholder. The `gitea-runner-config` Secret is
     owned by VaultwardenK8sSync (see the
     vaultwarden-k8s-sync app) — VKS polls a
     Vaultwarden (or Bitwarden-compatible) server for
     a Secure Note tagged with
     `namespaces=gitea-runner`,
     `secret-name=gitea-runner-config`, and
     `secret-key=registrationToken`, and writes the
     note's body into the Secret's `registrationToken`
     key. The apply step + the VKS sync step
     converge on the same Secret; the apply never
     overwrites the VKS-owned data.

Idempotency:
  - helm upgrade --install (default).
  - The runner is `ephemeral: true` so the registration
    is single-use; subsequent runs re-register.
  - The apply leaves the `gitea-runner-config` Secret
    alone. The chart's `secret.yaml` template uses
    `lookup` to skip creation if the Secret already
    exists, so a re-apply on a cluster where VKS has
    already populated the Secret doesn't clobber it.
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
                # The chart's secret.yaml creates the
                # Secret shell on first install (placeholder
                # registrationToken). The apply additionally
                # ensures the placeholder is set if the
                # Secret has been wiped — but never
                # overwrites a VKS-populated value.
                f"kubectl get/apply secret/{RUNNER_CONFIG_SECRET} "
                f"-n {NAMESPACE} (regression-guarded placeholder)",
            ],
            notes=[
                f"image: gitea/runner:{APP_VERSION}",
                "ephemeral: true (single-use registration)",
                "persistence: proxmox-lvm-thin PVC (cache + data)",
                (
                    "registration token source: VaultwardenK8sSync "
                    f"populates Secret={RUNNER_CONFIG_SECRET} "
                    f"(key=registrationToken). Operator creates a "
                    "Secure Note in the Vaultwarden web UI with "
                    "custom fields namespaces=gitea-runner, "
                    "secret-name=gitea-runner-config, "
                    "secret-key=registrationToken — the note body "
                    "is the Gitea runner registration token from "
                    "Site Administration > Actions > Runners > "
                    "Create new runner. See "
                    "docs/runbooks/setup-vaultwarden-sync.md."
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

        # 1. The registration token contract is codified by
        #    the static Secret `gitea-runner-config` (key
        #    `registrationToken`) in this namespace. The chart
        #    renders the Secret shell with a placeholder. We
        #    seed an empty token explicitly so the runner pod
        #    has a deterministic starting state. The operator
        #    (or the vaultwarden-k8s-sync app) is responsible
        #    for populating this Secret with a real token.

        # 2. helm install the local chart.
        # Note: we pass --wait=false because the runner pod
        # doesn't go Ready until the gitea-runner-config
        # Secret has a real registration token. Forcing
        # --wait here would fail the apply on a fresh install.
        result = ctx.helm.install_or_upgrade(
            release=RELEASE,
            chart=str(chart_dir),
            namespace=NAMESPACE,
            version=CHART_VERSION,
            values_files=values_for_helm,
            timeout_s=300.0,
            extra_args=("--wait=false",),
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

        # 5. The `gitea-runner-config` Secret is owned by
        #    VaultwardenK8sSync. The chart's secret.yaml
        #    template creates the Secret shell on first
        #    install (with a placeholder registrationToken)
        #    and subsequent helm upgrades leave it alone
        #    thanks to the `lookup` guard.
        #
        #    The apply takes a belt-and-braces approach:
        #    it inspects the live Secret and only writes
        #    a placeholder when the Secret is missing OR
        #    still carries the chart's placeholder value.
        #    A Secret that VKS has already populated (with
        #    the real Gitea registration token) is left
        #    alone. This is the same regression-guard
        #    pattern used by vaultwarden_k8s_sync.py: write
        #    only what's missing, never overwrite.
        live = kubectl.get(
            resource="secret",
            name=RUNNER_CONFIG_SECRET,
            namespace=NAMESPACE,
            jsonpath="{.data.registrationToken}",
        )
        existing_b64 = (live.stdout or "").strip()
        existing_text = ""
        if existing_b64:
            import base64

            try:
                existing_text = base64.b64decode(existing_b64).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                existing_text = ""
        placeholder = "PLACEHOLDER-TOKEN-OVERWRITTEN-BY-VAULTWARDEN-K8S-SYNC"
        if live.returncode == 0 and existing_text and existing_text != placeholder:
            ctx.logger.info(
                "gitea_runner.config_secret_owned_by_vks",
                secret=RUNNER_CONFIG_SECRET,
                namespace=NAMESPACE,
                note="VaultwardenK8sSync has populated the Secret; apply will not overwrite",
            )
        else:
            # Recreate the Secret shell with the placeholder.
            # We use `kubectl apply --server-side` with the
            # imperative object style so we always end up with
            # exactly one Secret. If VKS has since written a
            # real token, the next apply sees the populated
            # value and stops touching it.
            secret_yaml = (
                "apiVersion: v1\n"
                "kind: Secret\n"
                "metadata:\n"
                f"  name: {RUNNER_CONFIG_SECRET}\n"
                f"  namespace: {NAMESPACE}\n"
                "type: Opaque\n"
                "stringData:\n"
                f'  registrationToken: "{placeholder}"\n'
            )
            secret_apply = kubectl.apply(
                manifest=secret_yaml,
                namespace=NAMESPACE,
                server_side=True,
            )
            if secret_apply.returncode != 0:
                raise RuntimeError(
                    f"kubectl apply Secret={RUNNER_CONFIG_SECRET} "
                    f"failed: rc={secret_apply.returncode} "
                    f"stderr={secret_apply.stderr.strip()[:500]}"
                )
            ctx.logger.info(
                "gitea_runner.config_secret_seeded_with_placeholder",
                secret=RUNNER_CONFIG_SECRET,
                namespace=NAMESPACE,
                note="VKS will overwrite the placeholder on the next sync cycle",
            )

        # 6. Post-apply next step. The operator must:
        #    a. Finish Gitea first-boot (set admin password).
        #    b. Site Administration -> Actions -> Runners ->
        #       Create new runner. Copy the registration
        #       token (do NOT paste it into kubectl — VKS
        #       owns the Secret).
        #    c. In the Vaultwarden web UI, create a Secure
        #       Note whose body IS the registration token,
        #       and set the custom fields:
        #         namespaces = gitea-runner
        #         secret-name = gitea-runner-config
        #         secret-key = registrationToken
        #       VaultwardenK8sSync polls the server and
        #       writes the body into the gitea-runner-config
        #       Secret within one sync interval (~5 min by
        #       default). The runner pod picks up the new
        #       token via volume-mount refresh within ~30s.
        ctx.logger.info(
            "gitea_runner.waiting_for_vks_token",
            secret=RUNNER_CONFIG_SECRET,
            namespace=NAMESPACE,
            vks_custom_fields={
                "namespaces": NAMESPACE,
                "secret-name": RUNNER_CONFIG_SECRET,
                "secret-key": "registrationToken",
            },
            next_step=(
                "create a Secure Note in the Vaultwarden web UI "
                "with the custom fields namespaces/secret-name/"
                "secret-key printed in `vks_custom_fields` above; "
                "body = the Gitea runner registration token from "
                "Site Administration > Actions > Runners > "
                "Create new runner. VKS will populate "
                f"{RUNNER_CONFIG_SECRET} within one sync "
                "interval. See docs/runbooks/setup-vaultwarden-sync.md"
            ),
        )

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
        kubectl = KubectlRunner(kubeconfig=kubeconfig, logger=ctx.logger)
        ctx.kubectl = kubectl
        return kubectl


# Side-effect import: register on import.
register(GiteaRunnerApp)


__all__ = [
    "GiteaRunnerApp",
    "CHART_VERSION",
    "APP_VERSION",
    "NAMESPACE",
    "RUNNER_CONFIG_SECRET",
]
