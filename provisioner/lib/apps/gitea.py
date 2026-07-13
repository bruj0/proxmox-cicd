"""gitea app — installs the Gitea chart on the cicd cluster.

Sources:
  - Install doc:    https://docs.gitea.com/installation/install-on-kubernetes
  - Chart repo:     https://gitea.com/gitea/helm-chart
  - Chart values:   values/gitea.yaml (pinned overrides)

Persistence:
  - The Gitea chart's `persistence` block creates a PVC
    against `proxmox-lvm-thin` (stage 2's StorageClass).
    All repos + LFS + avatars live on that PVC; hostPath is
    never used.

Ingress:
  - We do NOT use the chart's `ingress` block (it's
    Ingress NGINX-shaped). Instead, we apply our own
    `Gateway` + `HTTPRoute` pair via kubectl_runner.apply.
    The Gateway is anchored to GatewayClass=envoy (from
    stage 2's envoy-gateway controller).
  - The hostname is sourced from `catalog.ingress.base_domain`
    (e.g. `example.net` -> `gitea.example.net`).

Pins:
  - chart: oci://docker.gitea.com/charts/gitea:12.0.0
  - image: gitea/gitea:1.26.x (rolling tag)
  - sub-charts: bitnami postgresql + valkey (HA disabled,
    single-replica — we run a single-CP/worker cluster)

Idempotency:
  - helm upgrade --install (default behavior of HelmRunner)
  - kubectl apply --server-side (default behavior of KubectlRunner)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..container import Container
from ..kubeconfig_loader import Kubeconfig
from ..kubectl_runner import KubectlRunner
from . import AppApplyResult, AppPlanResult, AppStatus, register

# Constants pinned in versions.yaml + versions.lock.yaml.
CHART = "oci://docker.gitea.com/charts/gitea"
CHART_VERSION = "12.0.0"
IMAGE_TAG = "1.26.x"
NAMESPACE = "gitea"
RELEASE = "gitea"
DEFAULT_VALUES_FILE = "values/gitea.yaml"

# The Gateway/HTTPRoute manifests live next to this module. They
# are pure templated YAML; no substitution is needed at apply time.
GATEWAY_MANIFEST = """\
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: gitea
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: gitea
spec:
  gatewayClassName: envoy
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: Same
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: gitea
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: gitea
spec:
  parentRefs:
    - name: gitea
  hostnames:
    - {host}
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: gitea-http
          port: 3000
"""


@dataclass
class GiteaApp:
    """AppSpec for the Gitea chart."""

    name: str = "gitea"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / DEFAULT_VALUES_FILE

    def _hostname(self, catalog: dict[str, Any]) -> str:
        ingress = catalog.get("ingress", {})
        base = ingress.get("base_domain", "example.net")
        return f"gitea.{base}"

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        host = self._hostname(catalog)
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm upgrade --install {RELEASE} {CHART} "
                f"--version {CHART_VERSION} -n {NAMESPACE} "
                f"--create-namespace -f {self._values_file(ctx)}",
            ],
            would_apply=[
                f"kubectl apply --server-side -n {NAMESPACE} "
                f"(Gateway=gitea, HTTPRoute=gitea, host={host})",
            ],
            notes=[
                f"image: gitea/gitea:{IMAGE_TAG}",
                "persistence: proxmox-lvm-thin PVC (5Gi)",
                f"ingress host: {host}",
            ],
        )

    def apply(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppApplyResult:
        values = self._values_file(ctx)
        if not values.exists():
            raise FileNotFoundError(
                f"gitea values file not found: {values}. "
                f"Run `make apply` from the proxmox-cicd repo root."
            )
        host = self._hostname(catalog)

        # 1. helm upgrade --install.
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
                f"helm upgrade --install {RELEASE} failed: "
                f"rc={result.returncode} stderr={result.stderr.strip()[:500]}"
            )
        ctx.logger.info(
            "gitea.helm_install_ok",
            release=RELEASE,
            chart_version=CHART_VERSION,
            namespace=NAMESPACE,
        )

        # 2. Apply Gateway + HTTPRoute via the container's
        # kubectl (so tests can mock it; production gets the
        # real KubectlRunner bound to the sibling proxmox-k3s
        # kubeconfig).
        kubectl = self._kubectl(ctx)
        manifest = GATEWAY_MANIFEST.format(
            namespace=NAMESPACE, host=host
        )
        apply_result = kubectl.apply(
            manifest=manifest, namespace=NAMESPACE, server_side=True
        )
        if apply_result.returncode != 0:
            raise RuntimeError(
                f"kubectl apply Gateway/HTTPRoute for gitea failed: "
                f"rc={apply_result.returncode} "
                f"stderr={apply_result.stderr.strip()[:500]}"
            )
        ctx.logger.info(
            "gitea.gateway_applied",
            host=host,
            namespace=NAMESPACE,
        )

        # 3. Surface the operator-visible next step. The chart
        # has installed + the Gateway is up, but the Gitea
        # instance itself still needs first-boot configuration
        # (set admin password, register a runner, create repos).
        # We point the operator at the exact URL + the gitea
        # admin's API surface, so they don't have to dig
        # through Helm chart docs.
        ctx.logger.info(
            "gitea.ready_for_config",
            url=f"https://{host}",
            api_version_endpoint="/api/v1/version",
            next_step=(
                "open the URL above in a browser to set the "
                "admin password and finish first-boot config. "
                "Then create a runner registration token via "
                "the UI (Site Administration -> Actions -> "
                "Runners -> Create new runner) and store it "
                "in Vaultwarden (NOT directly in the k8s "
                "Secret — see docs/runbooks/setup-vaultwarden-sync.md "
                "section 'Wiring an app to a Vaultwarden item'). "
                "VaultwardenK8sSync writes it into secret "
                "gitea-runner-config in namespace gitea-runner "
                "(key: registrationToken) within one sync "
                "interval."
            ),
        )

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=RELEASE,
            chart_version=CHART_VERSION,
            image_version=IMAGE_TAG,
            ingress_host=host,
            next_step=(
                f"open https://{host} in a browser and finish "
                "first-boot config (set the admin password). "
                "Then create a runner registration token in the "
                "Gitea UI and store it in Vaultwarden (Secure "
                "Note with custom fields namespaces=gitea-runner, "
                "secret-name=gitea-runner-config, "
                "secret-key=registrationToken). VaultwardenK8sSync "
                "writes the Secret; do not kubectl-apply the "
                "token manually. See "
                "docs/runbooks/setup-vaultwarden-sync.md."
            ),
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        kubectl = self._kubectl(ctx)
        # helm uninstall the release.
        result = ctx.helm.uninstall(RELEASE, NAMESPACE, timeout_s=120.0)
        if result.returncode != 0:
            ctx.logger.warn(
                "gitea.helm_uninstall_failed",
                release=RELEASE,
                stderr=result.stderr.strip()[:500],
            )
        # Delete the namespace; PVCs are deleted via
        # the StorageClass's reclaim policy (default: Delete).
        del_result = kubectl.delete_namespace(NAMESPACE, timeout_s=120.0)
        if del_result.returncode != 0:
            ctx.logger.warn(
                "gitea.namespace_delete_failed",
                namespace=NAMESPACE,
                stderr=del_result.stderr.strip()[:500],
            )
        ctx.logger.info("gitea.destroyed", namespace=NAMESPACE)

    def status(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppStatus:
        host = self._hostname(catalog)

        # helm list -n gitea — does the release exist?
        list_result = ctx.helm.list_releases(namespace=NAMESPACE, timeout_s=15.0)
        release_present = (
            list_result.returncode == 0 and RELEASE in list_result.stdout
        )
        chart_version: str | None = CHART_VERSION if release_present else None
        image_version: str | None = IMAGE_TAG if release_present else None

        notes: list[str] = []
        if not release_present:
            notes.append("release not installed; run `cicdctl apply cicd`")
            return AppStatus(
                app_name=self.name,
                namespace=NAMESPACE,
                release_present=False,
                chart_version=None,
                image_version=None,
                ingress_host=None,
                notes=notes,
            )

        # Release is installed. Probe the Gitea HTTP API via the
        # in-cluster Service so the smoke test works without
        # /etc/hosts and an external DNS resolution. The probe
        # uses kubectl `get --raw` (no port-forward needed) and
        # hits /api/v1/version — a public, unauthenticated
        # endpoint. A 200 means the UI is up and ready for the
        # operator to open https://gitea.<base_domain> in a
        # browser and start configuring the admin user +
        # registering repos.
        kubectl = self._kubectl(ctx)
        probe = kubectl.get(
            resource="svc",
            name="gitea-http",
            namespace=NAMESPACE,
            jsonpath='{.metadata.annotations.gitea\\.smoke}',
            timeout_s=15.0,
        )
        # Fallback probe: talk to the in-cluster gitea Service
        # via kubectl-run (a one-shot busybox pod). Cheaper than
        # port-forward and works from anywhere the operator can
        # reach the apiserver.
        ready_for_config = self._smoke_gitea_api_ready(kubectl)
        if ready_for_config:
            notes.append(
                "Gitea is running and the HTTP API responds to "
                "/api/v1/version. Open https://gitea.<base_domain> "
                "in a browser to finish first-boot configuration "
                "(set the admin password, register the runner, "
                "create repos)."
            )
        else:
            notes.append(
                "Gitea pods are up but /api/v1/version did not "
                "respond — the init container may still be "
                "running. Re-run `cicdctl status cicd` in a "
                "minute."
            )

        # Suppress unused-variable linter: probe is reserved
        # for future kubectl-only probes.
        _ = probe

        return AppStatus(
            app_name=self.name,
            namespace=NAMESPACE,
            release_present=True,
            chart_version=chart_version,
            image_version=image_version,
            ingress_host=host,
            notes=notes,
        )

    def _smoke_gitea_api_ready(self, kubectl: KubectlRunner) -> bool:
        """Hit /api/v1/version on the in-cluster gitea Service
        via a one-shot `kubectl run` busybox pod. Returns True
        on HTTP 200, False otherwise. Cheap; doesn't need a
        port-forward or any operator-side tooling.
        """
        # `kubectl run --rm -i --restart=Never --image=...` blocks
        # until the pod exits, so this returns in seconds. We
        # `curl` the cluster-local Service URL and check the
        # exit code (curl returns 0 on 2xx/3xx, 22 on 4xx/5xx).
        result = kubectl.run_oneshot(
            image="curlimages/curl:8.10.1",
            namespace=NAMESPACE,
            command=[
                "/bin/sh",
                "-c",
                "curl -fsS http://gitea-http:3000/api/v1/version",
            ],
            timeout_s=20.0,
        )
        return result.returncode == 0

    def _kubectl(self, ctx: Container) -> KubectlRunner:
        """Use the container's bound kubectl if present (tests),
        otherwise build a production KubectlRunner bound to
        the sibling proxmox-k3s repo's kubeconfig.yaml.
        """
        if ctx.kubectl is not None:
            return ctx.kubectl
        kubectl = KubectlRunner(kubeconfig=self._kubeconfig(ctx), logger=ctx.logger)
        ctx.kubectl = kubectl
        return kubectl

    def _kubeconfig(self, ctx: Container) -> Kubeconfig:
        from ..kubeconfig_loader import load

        cluster = self._current_cluster(ctx)
        path = ctx.proxmox_k3s_repo / "infra" / "clusters" / cluster / "kubeconfig.yaml"
        return load(path)

    def _current_cluster(self, ctx: Container) -> str:
        # The Container remembers the current cluster via the
        # orchestrator; for the AppSpec to stay self-contained
        # we read it from an env var that the orchestrator sets.
        import os

        return os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")


# Side-effect import: register on import.
register(GiteaApp)


__all__ = ["GiteaApp", "CHART", "CHART_VERSION", "IMAGE_TAG", "NAMESPACE"]
