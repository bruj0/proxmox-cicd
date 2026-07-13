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

# Chart constants. Pinned to chart 2.0.0 (matches appVersion 2.0.0;
# the chart repo's HEAD may carry a 0.1.0 + "latest" dev tag, but
# the published chart that the operator should consume is 2.0.0 from
# the OCI registry).
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

        # Resolve the (BW_CLIENTID, BW_CLIENTSECRET,
        # VAULTWARDEN__MASTERPASSWORD) triple + the
        # VAULTWARDEN__SERVERURL from a local .env file in
        # the operator's CWD, if one exists. The URL is
        # also fall-back-able to catalog["vaultwarden"].
        dotenv = self._load_dotenv(ctx.repo_root)

        # Resolution order for the server URL:
        #   1. .env VAULTWARDEN__SERVERURL (preferred)
        #   2. catalog.vaultwarden.server_url
        #   3. the values file's placeholder (last resort)
        server_url = (
            dotenv.get("VAULTWARDEN__SERVERURL", "")
            or (catalog.get("vaultwarden", {}) or {}).get(
                "server_url", ""
            )
            or ""
        )

        # Render the values file with the operator's URL
        # overlaid. We never modify the committed values
        # file in-place; instead we write a sibling
        # `.values-rendered.yaml` next to the operator's
        # kubeconfig (per-cluster, runtime state).
        rendered_values = self._render_values(values, server_url)

        creds = {
            "BW_CLIENTID": dotenv.get("BW_CLIENTID", ""),
            "BW_CLIENTSECRET": dotenv.get("BW_CLIENTSECRET", ""),
            "VAULTWARDEN__MASTERPASSWORD": dotenv.get(
                "VAULTWARDEN__MASTERPASSWORD", ""
            ),
        }

        ctx.logger.info(
            "vaultwarden_k8s_sync.helm_install_started",
            release=RELEASE,
            namespace=NAMESPACE,
            chart_version=CHART_VERSION,
            chart=CHART,
            server_url_source=(
                "env"
                if dotenv.get("VAULTWARDEN__SERVERURL")
                else "catalog" if server_url else "values"
            ),
            credentials_source=(
                ".env" if creds["BW_CLIENTID"] else "manual"
            ),
        )

        # 1. helm upgrade --install WITHOUT --wait. The sync
        #    pod doesn't go Ready until the BW_* Secret
        #    exists; on a fresh chart install that Secret
        #    is missing, so --wait would fail the apply
        #    pre-emptively. We seed the Secret in step 2
        #    after the chart has been rendered.
        result = ctx.helm.install_or_upgrade(
            release=RELEASE,
            chart=CHART,
            namespace=NAMESPACE,
            version=CHART_VERSION,
            values_files=(rendered_values,),
            timeout_s=300.0,
            extra_args=("--wait=false",),
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

        kubectl = self._kubectl(ctx)

        # 2. Seed the auth Secret. The chart's env.secrets
        #    references the Secret name `vaultwarden-
        #    kubernetes-secrets` (NOT `...-auth`, which is
        #    what auth-secret.yaml creates when api.enabled
        #    is true). We explicitly upsert an Opaque
        #    Secret here.
        #
        #    Important: only emit keys for which we have
        #    a value from .env. A bare re-apply with an
        #    empty VAULTWARDEN__MASTERPASSWORD would
        #    clobber a value the operator set out-of-band
        #    (via scripts/reseed-vks-creds.sh). kubectl
        #    apply merges stringData, so an empty key
        #    in the manifest would erase the live value.
        secret_yaml_lines = [
            "apiVersion: v1",
            "kind: Secret",
            "metadata:",
            f"  name: {NAMESPACE}",
            f"  namespace: {NAMESPACE}",
            "  labels:",
            "    app.kubernetes.io/name: vaultwarden-kubernetes-secrets",
            "    app.kubernetes.io/instance: vaultwarden-kubernetes-secrets",
            "type: Opaque",
            "stringData:",
        ]
        seeded_keys: list[str] = []
        for key in (
            "BW_CLIENTID",
            "BW_CLIENTSECRET",
            "VAULTWARDEN__MASTERPASSWORD",
        ):
            value = creds[key]
            if value:
                secret_yaml_lines.append(f"  {key}: {value}")
                seeded_keys.append(key)
        # Always have *something* under stringData; if
        # .env had no creds at all, emit an empty body
        # so the apply still creates a Secret shell
        # (the chart's env.secrets reference will fail
        # until the operator populates it, but the
        # next-step will tell them how).
        if not seeded_keys:
            secret_yaml_lines.append("  # populate via .env or reseed-vks-creds.sh")
        secret_yaml = "\n".join(secret_yaml_lines) + "\n"
        secret_apply = kubectl.apply(
            manifest=secret_yaml,
            namespace=NAMESPACE,
            server_side=True,
        )
        if secret_apply.returncode != 0:
            raise RuntimeError(
                f"kubectl apply Secret={NAMESPACE} in {NAMESPACE} "
                f"failed: rc={secret_apply.returncode} "
                f"stderr={secret_apply.stderr.strip()[:500]}"
            )
        # Record whether all three credential keys were
        # populated; surface this in the post-apply next-step.
        all_present = all(
            creds[k] for k in (
                "BW_CLIENTID",
                "BW_CLIENTSECRET",
                "VAULTWARDEN__MASTERPASSWORD",
            )
        )
        ctx.logger.info(
            "vaultwarden_k8s_sync.auth_secret_seeded",
            secret=f"{NAMESPACE}/{NAMESPACE}",
            seeded_keys=seeded_keys,
            credentials_populated=all_present,
            credentials_source=(
                ".env" if creds["BW_CLIENTID"] else "manual"
            ),
        )

        # 3. Wait (best-effort, 30s) for the Deployment. Now
        #    that the Secret is seeded with non-empty values
        #    (if the operator provided them), the pod should
        #    go Ready fast. We don't fail the apply if it
        #    doesn't — the next-step tells the operator
        #    what to do.
        wait = kubectl.wait_deployments_available(
            namespace=NAMESPACE,
            label_selector="app.kubernetes.io/name=vaultwarden-kubernetes-secrets",
            timeout_s=60.0,
        )
        if wait.returncode != 0:
            ctx.logger.warn(
                "vaultwarden_k8s_sync.deployments_not_available",
                stderr=wait.stderr.strip()[:500],
                resolution=(
                    "the BW_* Secret may still be empty "
                    "or the BW_CLIENTID/BW_CLIENTSECRET/VAULTWARDEN__MASTERPASSWORD "
                    "fields are empty. See apply.next_step."
                ),
            )

        if not all_present:
            # Manual next-step: the operator must populate
            # the Secret themselves.
            next_step_msg = (
                f"populate Secret {NAMESPACE}/{NAMESPACE} with "
                f"BW_CLIENTID, BW_CLIENTSECRET, and "
                f"VAULTWARDEN__MASTERPASSWORD. From your Vaultwarden "
                f"account: Settings -> Account -> API Key, copy "
                f"the client_id + client_secret. Then: "
                f"kubectl -n {NAMESPACE} create secret generic "
                f"{NAMESPACE} --from-literal=BW_CLIENTID=<uuid> "
                f"--from-literal=BW_CLIENTSECRET=<secret> "
                f"--from-literal=VAULTWARDEN__MASTERPASSWORD=<password> "
                f"--dry-run=client -o yaml | kubectl apply -f -. "
                f"The sync service will start polling within "
                f"~30s once the Secret is updated."
            )
        else:
            # Auto-seeded from .env; monitor for sync
            # errors and confirm the operator id has the
            # right org/collection.
            next_step_msg = (
                f"credentials auto-seeded from .env. Verify "
                f"the sync service has reach to {server_url} "
                f"and that the operator id "
                f"(VAULTWARDEN__ORGANIZATIONID / "
                f"VAULTWARDEN__COLLECTIONID in values.yaml) "
                f"is set before turning on the dashboard. "
                f"kubectl -n {NAMESPACE} logs -l "
                f"app.kubernetes.io/name=vaultwarden-kubernetes-secrets "
                f"to watch the first sync cycle."
            )
        ctx.logger.info(
            "vaultwarden_k8s_sync.post_install",
            next_step=next_step_msg,
        )

        return AppApplyResult(
            app_name=self.name,
            namespace=NAMESPACE,
            release=RELEASE,
            chart_version=CHART_VERSION,
            image_version=APP_VERSION,
            ingress_host=None,
            next_step=next_step_msg,
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

    @staticmethod
    def _load_dotenv(repo_root: Path) -> dict[str, str]:
        """Best-effort read of the VKS-relevant keys from a
        `.env` file in `repo_root` (typically the operator's
        CWD when running cicdctl).

        Accepted key names (case-insensitive, all aliased to
        their canonical VKS form):
          - BW_CLIENTID  / CLIENT_ID         -> BW_CLIENTID
          - BW_CLIENTSECRET / CLIENT_SECRET  -> BW_CLIENTSECRET
          - VAULTWARDEN__MASTERPASSWORD /
            VAULTWARDEN_MASTERPASSWORD /
            MASTER_PASSWORD                  -> VAULTWARDEN__MASTERPASSWORD
          - VAULTWARDEN__SERVERURL /
            VAULTWARDEN_SERVERURL /
            VAULTWARDEN_URL /
            BITWARDEN_URL                    -> VAULTWARDEN__SERVERURL

        We parse with stdlib only (no python-dotenv dep). Format
        is a one-per-line KEY=VALUE pair (no quoting/escape
        handling beyond stripping leading/trailing whitespace
        and quotes).
        """
        env_path = repo_root / ".env"
        out: dict[str, str] = {
            "BW_CLIENTID": "",
            "BW_CLIENTSECRET": "",
            "VAULTWARDEN__MASTERPASSWORD": "",
            "VAULTWARDEN__SERVERURL": "",
        }
        if not env_path.exists():
            return out
        key_aliases: dict[str, str] = {
            "bw_clientid": "BW_CLIENTID",
            "client_id": "BW_CLIENTID",
            "bw_clientsecret": "BW_CLIENTSECRET",
            "client_secret": "BW_CLIENTSECRET",
            "vaultwarden__masterpassword": "VAULTWARDEN__MASTERPASSWORD",
            "vaultwarden_masterpassword": "VAULTWARDEN__MASTERPASSWORD",
            "master_password": "VAULTWARDEN__MASTERPASSWORD",
            "vaultwarden__serverurl": "VAULTWARDEN__SERVERURL",
            "vaultwarden_serverurl": "VAULTWARDEN__SERVERURL",
            "vaultwarden_url": "VAULTWARDEN__SERVERURL",
            "bitwarden_url": "VAULTWARDEN__SERVERURL",
        }
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            canonical = key_aliases.get(key)
            if canonical is not None:
                out[canonical] = value
        return out

    @staticmethod
    def _render_values(
        committed_values: Path, server_url: str
    ) -> Path:
        """Build a runtime values file with the operator's
        `VAULTWARDEN__SERVERURL` overlaid on top of the
        committed values file. Writes a sibling
        `vaultwarden-kubernetes-secrets.values-rendered.yaml`
        next to the committed file in `values/` (operator-
        local — the file should be added to .gitignore
        or cleaned up by `git clean -fX values/`). Falls
        back to the committed file unchanged if no URL
        was supplied.
        """
        if not server_url:
            return committed_values
        # We use a simple textual replacement because the
        # committed file is a flat YAML fragment with
        # predictable formatting; full YAML re-encoding
        # would risk losing comments + the chart's
        # secretKeyRef anchor block.
        text = committed_values.read_text()
        new_line = f'    VAULTWARDEN__SERVERURL: "{server_url}"'
        if 'VAULTWARDEN__SERVERURL:' in text:
            out_lines: list[str] = []
            replaced = False
            for raw in text.splitlines(keepends=True):
                stripped = raw.lstrip()
                if (
                    not replaced
                    and stripped.startswith("VAULTWARDEN__SERVERURL:")
                ):
                    out_lines.append(new_line + "\n")
                    replaced = True
                else:
                    out_lines.append(raw)
            text = "".join(out_lines)
        else:
            # Append a top-level env.config block. The
            # chart treats `env.config` as a dict, so this
            # is safe to add.
            text += (
                "\n# Auto-added by vaultwarden_k8s_sync.apply():\n"
                "env:\n"
                "  config:\n"
                f"{new_line}\n"
            )
        out_path = committed_values.with_name(
            committed_values.stem + ".values-rendered.yaml"
        )
        out_path.write_text(text)
        return out_path

    def _kubectl(self, ctx: Container) -> Any:
        # Late import to avoid a circular dep at module load.
        from ..kubectl_runner import KubectlRunner

        if ctx.kubectl is not None:
            return ctx.kubectl
        from ..kubeconfig_loader import Kubeconfig, load
        import os

        # Resolve the cluster name from the env var the
        # orchestrator sets. Falls back to "cicd" so the
        # AppSpec remains usable from a test fixture.
        cluster = os.environ.get("PROXMOX_CICD_CLUSTER", "cicd")
        path = (
            ctx.proxmox_k3s_repo
            / "infra"
            / "clusters"
            / cluster
            / "kubeconfig.yaml"
        )
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
