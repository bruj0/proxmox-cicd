"""gitea-runner app — installs the in-repo gitea-runner chart.

This is the ONE chart this repo owns. The upstream Gitea
project doesn't publish an official runner helm chart, so
we wrap the official `gitea/runner:1.0.8-dind` docker image
in a small per-repo chart under
`infra/charts/gitea-runner/`.

Sources:
  - https://docs.gitea.com/runner/1.0.8/
  - https://gitea.com/gitea/act-runner
  - https://gitea-runner/examples/kubernetes/ (rootless
    and statefulset-dind examples upstream)

Installation flow:
  1. Vaultwarden-backed credentials. The orchestrator
     reads the gitea admin password from the Vaultwarden
     Secure Note that ``apps/gitea.py`` seeded during its
     own apply step
     (namespaces=gitea, secret-name=gitea-admin-password,
     secret-key=password). With those credentials in hand,
     the orchestrator calls the Gitea admin API
     ``GET /api/v1/admin/runners/registration-token`` to
     mint a fresh runner registration token. The token
     body is then pushed to Vaultwarden as a Secure Note
     carrying the VKS triple
     (namespaces=gitea-runner,
     secret-name=gitea-runner-config,
     secret-key=registrationToken). VaultwardenK8sSync
     reconciles the cluster Secret from that note within
     one sync interval; the runner pod picks the token up
     via the chart's `/etc/runner/token` file mount.

     No host-side token cache. The contract is
     "Vaultwarden must be working before any app can
     install": if VW is down, the apply hard-fails.

  2. helm install the runner chart. The chart ships a
     `secret.yaml` template that creates the
     `gitea-runner-gitea-runner-config` Secret shell with
     a placeholder `registrationToken` value on first
     install, and a `statefulset.yaml` template that
     mounts the Secret as a volume at `/etc/runner/token`
     — the `registrationToken` key becomes the file
     content. Two PVCs are also created via
     `volumeClaimTemplates`:

       * `runner-data-<pod>` — mounted at `/data`. Holds
         the `.runner` registration file (so the runner
         re-attaches to its existing `action_runner` row
         instead of registering a new one on every pod
         start).
       * `docker-data-<pod>` — mounted at
         `/var/lib/docker`. The bundled Docker daemon's
         image cache. Without it, every pod recreation
         re-pulls every job's image.

  3. Wait for the runner StatefulSet to be Available.
     The runner pod stays in CrashLoopBackOff until
     the Secret has a non-empty registrationToken;
     VKS reconciles the placeheld Secret within one
     sync tick so the pod comes back to Ready shortly
     after the apply completes.

  4. The apply does NOT seed the Secret with a
     placeholder when VKS has already populated it.
     The `gitea-runner-gitea-runner-config` Secret is
     owned by VaultwardenK8sSync (see the
     vaultwarden-k8s-sync app) — VKS polls a
     Vaultwarden (or Bitwarden-compatible) server for
     a Secure Note tagged with
     `namespaces=gitea-runner`,
     `secret-name=gitea-runner-config`, and
     `secret-key=registrationToken`, and writes the
     note's body into the Secret's `registrationToken`
     key. The apply step + the VKS sync step converge
     on the same Secret; the apply never overwrites
     the VKS-owned data.

Idempotency:
  - helm upgrade --install (default).
  - The chart is non-ephemeral (ephemeral: false) and
    persists `/data` to a PVC. Subsequent applies do
    NOT re-mint via the Gitea admin API: the
    Vaultwarden cipher seeded in step 1 is preserved
    across applies and the runner re-attaches via the
    `.runner` file. (Gitea OSS forces run-once even
    with `ephemeral: false`; the chart sets the flag
    anyway so when this is migrated to Gitea
    Enterprise the `.runner`-file re-attach path Just
    Works without chart changes.)
  - The apply only writes the placeholder Secret when
    the live Secret is missing or carries the chart's
    known placeholder value; a Secret that VKS has
    already populated is left untouched.

Why dind (root) instead of dind-rootless:
  The chart uses `gitea/runner:1.0.8-dind` (Docker-in-
  Docker as UID 0 inside the container) rather than
  `gitea/runner:1.0.8-dind-rootless` (UID 1000). The
  rootless variant fails on stock k3s with
  `failed to start the child: fork/exec /proc/self/exe:
  operation not permitted` because rootlesskit needs
  CAP_SYS_ADMIN inside the container's user namespace,
  which k3s strips even with `--privileged` + seccomp
  Unconfined. The upstream
  `gitea-runner/examples/kubernetes/statefulset-dind.yaml`
  example uses the root flavour for the same reason.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..container import Container
from . import AppApplyResult, AppPlanResult, AppStatus, register
from .base import BaseApp

# Chart constants used to live here as module-level
# `NAMESPACE`, `RELEASE`, `CHART_VERSION`,
# `APP_VERSION`, `DEFAULT_VALUES_FILE`. WP13 moved
# them onto `GiteaRunnerApp` as class attributes.
# The `tests/test_app_class_attributes.py` drift
# test pins the values against `shipped.yaml`.

# The Secret name the chart creates / mounts.
#
# The chart's ``$secretName`` template builds
# ``<fullname>-config`` where fullname is the
# concatenation of .Release.Name + .Chart.Name
# (truncated, dash-trimmed):
#
#   release="gitea-runner" + chart="gitea-runner"
#     = "gitea-runner-gitea-runner"
#   + "-config"
#     = "gitea-runner-gitea-runner-config"
#
# If the orchestrator diverges from this exact name
# the runner pod will keep mounting the empty
# placeholder (volume ``token`` -> ``Secret has no
# ``registrationToken`` key``) and CrashLoop forever.
RUNNER_CONFIG_SECRET = "gitea-runner-gitea-runner-config"

# VKS contract: same triple the operator used to type into
# the Vaultwarden web UI before the orchestrator took over.
# Must equal RUNNER_CONFIG_SECRET exactly.
VKS_TRIPLE_NAMESPACE = "gitea-runner"
VKS_TRIPLE_SECRET_NAME = "gitea-runner-gitea-runner-config"
VKS_TRIPLE_SECRET_KEY = "registrationToken"

# Skip flag for CI / unit-test runs without Vaultwarden.
# Mirrors catalog.vaultwarden.skip_admin_seed in apps/gitea.py.
VW_SKIP_FLAG = "skip_runner_seed"


class GiteaRunnerApp(BaseApp):
    """AppSpec for the gitea-runner chart."""

    name = "gitea-runner"
    namespace = "gitea-runner"
    release = "gitea-runner"
    chart = "./infra/charts/gitea-runner"
    chart_version = "0.2.0"
    image_version = "1.0.8-dind"
    default_values_file = "values/gitea-runner.yaml"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / self.default_values_file

    def _chart_dir(self, ctx: Container) -> Path:
        return ctx.repo_root / "infra" / "charts" / "gitea-runner"

    def _gitea_instance_url(self, catalog: dict[str, Any]) -> str:
        """The runner polls the in-cluster gitea Service, not
        the public ingress. Hard-coded to the chart's expected
        service name + namespace.
        """
        return "http://gitea-http.gitea.svc.cluster.local:3000"

    def _gitea_public_url(self, catalog: dict[str, Any]) -> str:
        """The orchestrator itself talks to Gitea via the
        public-facing hostname (terminated by Envoy Gateway
        → the in-cluster gitea-http Service). Mirrors the
        hostname derivation in apps/gitea.py::_hostname so
        that mint-from-API calls hit the same URL the
        operator's browser uses.
        """
        ingress = catalog.get("ingress", {}) or {}
        base = ingress.get("base_domain", "example.net")
        return f"https://gitea.{base}"

    def plan(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppPlanResult:
        return AppPlanResult(
            app_name=self.name,
            would_install=[
                f"helm upgrade --install {self.release} "
                f"<repo>/gitea-runner (local chart, version "
                f"{self.chart_version}) -n {self.namespace}",
            ],
            would_apply=[
                f"kubectl get/apply secret/{RUNNER_CONFIG_SECRET} "
                f"-n {self.namespace} (regression-guarded placeholder)",
                f"Vaultwarden Secure Note seeded with VKS triple "
                f"namespaces={VKS_TRIPLE_NAMESPACE}, "
                f"secret-name={VKS_TRIPLE_SECRET_NAME}, "
                f"secret-key={VKS_TRIPLE_SECRET_KEY} "
                f"(body = freshly-minted runner registration "
                f"token from the Gitea admin API)",
            ],
            notes=[
                f"image: gitea/runner:{self.image_version}",
                "workload: StatefulSet, replicas=2 (one per "
                "k3s node; bump per cluster)",
                "ephemeral: false (non-ephemeral — the runner "
                "persists its .runner registration file to a "
                "PVC and re-attaches to the same Gitea row on "
                "every pod start instead of inserting a new "
                "`action_runner` row each time)",
                "container: gitea/runner:1.0.8-dind (root "
                "Docker-in-Docker flavour; bundled dockerd in "
                "each pod, no host docker socket). The rootless "
                "flavour is not viable on stock k3s — see the "
                "module docstring for the rootlesskit "
                "`fork/exec /proc/self/exe: operation not "
                "permitted` failure mode we hit when we tried.",
                "persistence: proxmox-lvm-thin PVCs (1×1Gi "
                "for /data holding the .runner file, 1×20Gi "
                "for /var/lib/docker holding the image cache; "
                "both are stable across pod restarts because "
                "the StatefulSet owns them via "
                "volumeClaimTemplates)",
                "probes: HTTP GET /healthz on port 8088 (the "
                "runner daemon's built-in metrics/healthz "
                "endpoint; `/healthz` is the real path, "
                "`/-/ready` does not exist in gitea-runner "
                "1.0.8 and was a stale assumption in earlier "
                "versions of this chart)",
                (
                    "registration token source: the orchestrator "
                    "mints the token from the Gitea admin API "
                    "(GET /api/v1/admin/runners/registration-token) "
                    "using gitea_admin credentials read from "
                    "Vaultwarden, then writes the body into a "
                    "Secure Note carrying VKS triple "
                    f"namespaces={VKS_TRIPLE_NAMESPACE}, "
                    f"secret-name={VKS_TRIPLE_SECRET_NAME}, "
                    f"secret-key={VKS_TRIPLE_SECRET_KEY}. "
                    "VaultwardenK8sSync reconciles the cluster "
                    f"Secret={RUNNER_CONFIG_SECRET} from that note "
                    "within one sync interval (~30s). The contract "
                    "is \"Vaultwarden must be working before any "
                    "app can install\" — the apply hard-fails if "
                    "the admin-pw cipher or a live Vaultwarden is "
                    "unavailable."
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

        # 1. Provision the runner registration token in
        #    Vaultwarden BEFORE the chart install. The chart
        #    creates a Secret shell with a placeholder
        #    registrationToken at install time; VKS reconciles
        #    that placeholder with the cipher we push here
        #    within one sync tick (~30s). By the time the helm
        #    install completes and the runner pod attempts to
        #    register, VKS has already overwritten the
        #    placeholder with the real token. Hard-fail on
        #    any Vaultwarden / Gitea failure (the operator's
        #    contract: "VW must be working before any app
        #    can install").
        self._ensure_runner_token_in_vaultwarden(ctx, catalog)

        # 2. helm install the local chart.
        # Note: we pass --wait=false because the runner pod
        # doesn't go Ready until the gitea-runner-config
        # Secret has a real registration token. Forcing
        # --wait here would fail the apply on a fresh install.
        result = ctx.helm.install_or_upgrade(
            release=self.release,
            chart=str(chart_dir),
            namespace=self.namespace,
            version=self.chart_version,
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
            release=self.release,
            namespace=self.namespace,
            chart_version=self.chart_version,
        )

        # 4. Wait for the runner StatefulSet to be Available.
        # We use the generic kubectl.wait() (against
        # `statefulset/<release>`) rather than the
        # Deployment-specific wait_deployments_available()
        # because the chart is a StatefulSet — Deployments
        # get rolled by helm upgrades but StatefulSets have
        # per-replica Pod identity and PVC ownership, so
        # `--wait=false` on the helm upgrade leaves the
        # StatefulSet untouched and we wait for it
        # afterwards.
        wait = kubectl.wait(
            resource="statefulset",
            name=self.release,
            namespace=self.namespace,
            condition="condition=Available=true",
            timeout_s=180.0,
        )
        if wait.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.statefulset_not_available",
                stderr=wait.stderr.strip()[:500],
                note=(
                    "the runner pod may take a while to come "
                    "Ready on a fresh Gitea instance; the "
                    "helm install completed successfully "
                    "and VKS will reconcile the registration "
                    "token within one sync tick"
                ),
            )

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
            namespace=self.namespace,
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
                namespace=self.namespace,
                note="VaultwardenK8sSync has populated the Secret; apply will not overwrite",
            )
        else:
            # Recreate the Secret shell with the placeholder.
            # We use `kubectl apply --server-side` with the
            # imperative object style so we always end up with
            # exactly one Secret. If VKS has since written a
            # real token, the next apply sees the populated
            # value and stops touching it.
            # WP5 — moved out of an inline f-string
            # into `apps/templates/gitea-runner/
            # registration-secret.yaml`. The
            # placeholder is the known regression-guard
            # token that VKS replaces on its first
            # sync cycle.
            secret_yaml = self._render_template(
                "registration-secret.yaml",
                secret_name=RUNNER_CONFIG_SECRET,
                namespace=self.namespace,
                placeholder=placeholder,
            )
            secret_apply = kubectl.apply(
                manifest=secret_yaml,
                namespace=self.namespace,
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
                namespace=self.namespace,
                note="VKS will overwrite the placeholder on the next sync cycle",
            )

        # 6. Post-apply next step. The orchestrator
        #    already minted the token via the Gitea admin
        #    API and pushed it to Vaultwarden; the chart's
        #    secret.yaml rendered the Secret shell, and
        #    VKS is reconciling the cluster Secret from
        #    the cipher within one sync tick (~30s).
        #    The runner pod will pick the token up from
        #    the volume mount once that reconcile
        #    completes. Operator next step: confirm the
        #    runner registers (kubectl logs, Site Admin
        #    -> Actions -> Runners) within ~1 minute.
        ctx.logger.info(
            "gitea_runner.token_reconciling",
            secret=RUNNER_CONFIG_SECRET,
            namespace=self.namespace,
            vks_triple={
                "namespaces": VKS_TRIPLE_NAMESPACE,
                "secret-name": VKS_TRIPLE_SECRET_NAME,
                "secret-key": VKS_TRIPLE_SECRET_KEY,
            },
            next_step=(
                "wait ~30s for VaultwardenK8sSync to reconcile "
                f"the cluster Secret {RUNNER_CONFIG_SECRET} "
                "from the Vaultwarden Secure Note the "
                "orchestrator just pushed; the runner pod "
                "should register with Gitea within ~1 minute. "
                "If it does not, check `kubectl logs -n "
                f"{self.namespace}` for the runner-side error "
                "and verify in Site Administration -> "
                "Actions -> Runners in the Gitea UI."
            ),
        )

        return AppApplyResult(
            app_name=self.name,
            namespace=self.namespace,
            release=self.release,
            chart_version=self.chart_version,
            image_version=self.image_version,
            ingress_host=None,
            next_step=(
                f"wait ~30s for VaultwardenK8sSync to reconcile "
                f"the cluster Secret {RUNNER_CONFIG_SECRET} "
                "from the Vaultwarden Secure Note the "
                "orchestrator just pushed; the runner pod "
                "should register with Gitea within ~1 minute. "
                "Verify in Site Administration -> Actions -> "
                "Runners in the Gitea UI."
            ),
        )

    def destroy(self, ctx: Container, catalog: dict[str, Any]) -> None:
        kubectl = self._kubectl(ctx)
        # Uninstall the helm release first (this deletes the
        # Deployment + Service + RBAC).
        helm_result = ctx.helm.uninstall(self.release, self.namespace, timeout_s=120.0)
        if helm_result.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.helm_uninstall_failed",
                release=self.release,
                stderr=helm_result.stderr.strip()[:500],
            )
        # Then delete the namespace (which deletes the
        # BitwardenSecret CR + ConfigMap).
        del_result = kubectl.delete_namespace(self.namespace, timeout_s=120.0)
        if del_result.returncode != 0:
            ctx.logger.warn(
                "gitea_runner.namespace_delete_failed",
                namespace=self.namespace,
                stderr=del_result.stderr.strip()[:500],
            )
        ctx.logger.info("gitea_runner.destroyed", namespace=self.namespace)

    def status(
        self, ctx: Container, catalog: dict[str, Any]
    ) -> AppStatus:
        list_result = ctx.helm.list_releases(namespace=self.namespace, timeout_s=15.0)
        release_present = (
            list_result.returncode == 0 and self.release in list_result.stdout
        )
        notes: list[str] = []
        if not release_present:
            notes.append("release not installed; run `cicdctl apply cicd`")
        return AppStatus(
            app_name=self.name,
            namespace=self.namespace,
            release_present=release_present,
            chart_version=self.chart_version if release_present else None,
            image_version=self.image_version if release_present else None,
            ingress_host=None,
            notes=notes,
        )

    # ---------------------------------------------------- runner token flow

    def _ensure_runner_token_in_vaultwarden(
        self,
        ctx: Container,
        catalog: dict[str, Any],
    ) -> None:
        """Mint a fresh runner registration token from the
        Gitea admin API and push it to Vaultwarden as a
        Secure Note with VKS triple (namespaces=
        gitea-runner, secret-name=gitea-runner-config,
        secret-key=registrationToken). VaultwardenK8sSync
        reconciles the cluster Secret from that note
        within one sync interval; the runner pod picks the
        token up via the chart's `/etc/runner/token` file
        mount.

        Hard-fails on any Vaultwarden / Gitea failure
        (operator's contract: "VW must be working before
        any app can install"). Skips silently when
        ``catalog.vaultwarden.skip_runner_seed`` is true
        (used by the orchestrator's unit tests).

        Idempotent: if a Secure Note carrying the same
        VKS triple already exists, the apply is a no-op
        (the operator may have manually edited the body
        and we shouldn't clobber it). Hard-fails on
        duplicates (>1 cipher matching the triple) because
        VKS picks arbitrarily among same-triple ciphers
        and that's confusing to debug.

        No host-side cache of the token. The single
        source of truth is the Vaultwarden cipher; the
        orchestrator re-mints from Gitea on every apply
        to keep the path stateless across hosts.
        """
        vw_cfg = catalog.get("vaultwarden", {}) or {}
        if vw_cfg.get(VW_SKIP_FLAG):
            ctx.logger.info(
                "gitea_runner.vaultwarden_skipped",
                reason=f"catalog.vaultwarden.{VW_SKIP_FLAG}=true",
                note=(
                    "cluster Secret placeholder path remains; "
                    "the Secure Note was NOT pushed"
                ),
            )
            return

        # 1. Read the gitea admin password from Vaultwarden.
        #    The cipher was seeded by apps/gitea.py during
        #    its own apply step; we look it up here rather
        #    than threading it through the catalog. The
        #    VKS triple for the admin cipher is fixed:
        #    namespaces=gitea,
        #    secret-name=gitea-admin-password,
        #    secret-key=password.
        admin_creds = self._fetch_gitea_admin_from_vaultwarden(
            ctx, catalog
        )
        if not admin_creds:
            raise RuntimeError(
                "cannot read Gitea admin credentials from "
                "Vaultwarden: no Secure Note with VKS triple "
                "(namespaces=gitea, secret-name=gitea-admin-password, "
                "secret-key=password) was found. The gitea app "
                "must be applied first so it seeds the admin-pw "
                "cipher; the apply path is order-dependent on "
                "purpose (gitea → gitea-runner → vaultwarden-k8s-sync). "
                "See docs/runbooks/setup-vaultwarden-sync.md."
            )
        admin_username, admin_password, client = admin_creds

        # 2. Idempotency check FIRST. Scan Vaultwarden for
        #    an existing cipher that already carries the
        #    runner's VKS triple. If exactly one matches,
        #    skip BOTH the Gitea admin API call AND the
        #    Vaultwarden POST — the apply is a no-op at the
        #    network level and the token-counter stays put.
        #    This is the difference between "Vaultwarden
        #    dedup" (cheap) and "Gitea admin dedup"
        #    (involves every apply incrementing Gitea's
        #    registration-token counter and then discarding
        #    the freshly-minted token).
        from provisioner.lib.vaultwarden import (
            build_secure_note_payload,
            vks_triple,
        )

        existing = client.list_ciphers()
        matching: list[dict[str, Any]] = []
        for c in existing:
            triple: dict[str, str] = {}
            for i in range(len(c.get("fields") or [])):
                try:
                    k = client.decrypt_cipher_field_name(c, index=i)
                    triple[k] = client.decrypt_cipher_field(c, name=k)
                except Exception:
                    continue
            if (
                triple.get("namespaces") == VKS_TRIPLE_NAMESPACE
                and triple.get("secret-name")
                == VKS_TRIPLE_SECRET_NAME
                and triple.get("secret-key") == VKS_TRIPLE_SECRET_KEY
            ):
                matching.append(c)
        if len(matching) > 1:
            ids = ", ".join(c.get("id", "?") for c in matching)
            raise RuntimeError(
                f"refusing to seed runner token: {len(matching)} "
                f"Vaultwarden ciphers already match VKS triple "
                f"(namespaces={VKS_TRIPLE_NAMESPACE}, "
                f"secret-name={VKS_TRIPLE_SECRET_NAME}, "
                f"secret-key={VKS_TRIPLE_SECRET_KEY}). "
                f"VaultwardenK8sSync would pick arbitrarily "
                f"among them. Delete duplicates in the "
                f"Vaultwarden web UI and re-apply. cipher ids: {ids}"
            )
        if matching:
            ctx.logger.info(
                "gitea_runner.vaultwarden_skipped",
                reason="cipher with matching VKS triple already exists",
                namespace=VKS_TRIPLE_NAMESPACE,
                secret_name=VKS_TRIPLE_SECRET_NAME,
                secret_key=VKS_TRIPLE_SECRET_KEY,
                # Visible signal that the Gitea admin
                # API was NOT hit on this apply — the
                # orchestrator is a no-op here.
                gitea_admin_api_called=False,
            )
            return

        # 3. Mint a fresh registration token via the Gitea
        #    admin API. Reached only when no matching cipher
        #    exists in Vaultwarden, so this call runs at
        #    most once per Vaultwarden state. Uses the
        #    HTTPS-on-public-hostname URL so the orchestrator
        #    exercises the same ingress the operator's
        #    browser does; Envoy Gateway terminates TLS
        #    upstream.
        gitea_url = self._gitea_public_url(catalog)
        token = self._mint_runner_token_from_gitea_api(
            ctx, gitea_url, admin_username, admin_password
        )
        if not token:
            raise RuntimeError(
                f"Gitea admin API returned an empty "
                f"registration token (POSTed to "
                f"{gitea_url}/api/v1/admin/runners/registration-token). "
                f"Check that gitea_admin has Site Admin role; "
                f"if you just rotated the admin password in "
                f"Vaultwarden, allow up to 30s for VKS to "
                f"reconcile it back to the in-cluster Secret "
                f"before retrying the gitea-runner apply."
            )

        payload = build_secure_note_payload(
            note_name="gitea runner registration token",
            body_text=token,
            custom_fields=vks_triple(
                namespace=VKS_TRIPLE_NAMESPACE,
                secret_name=VKS_TRIPLE_SECRET_NAME,
                secret_key=VKS_TRIPLE_SECRET_KEY,
            ),
            user_key=client.user_key,
        )
        client.create_cipher(payload)

        ctx.logger.info(
            "gitea_runner.vaultwarden_seeded",
            namespace=VKS_TRIPLE_NAMESPACE,
            secret_name=VKS_TRIPLE_SECRET_NAME,
            secret_key=VKS_TRIPLE_SECRET_KEY,
            token_length=len(token),
        )

    def _fetch_gitea_admin_from_vaultwarden(
        self,
        ctx: Container,
        catalog: dict[str, Any],
    ) -> tuple[str, str, Any] | None:
        """Helper: log in to Vaultwarden, find the cipher
        carrying the gitea-admin-pw triple, decrypt its
        body, return (username, password). Returns None
        when no matching cipher exists; raises on any
        other failure (login error, decrypt error,
        Vaultwarden unreachable).

        The tuple carries a ``client`` attribute (the
        VaultwardenClient we logged in as) so the caller
        can reuse the authenticated session for the
        follow-up POST without a second login cycle. This
        is a slightly unusual shape but it saves a 5s
        /identity/connect/token round-trip on every
        apply and matches the cloudflared seeding
        pattern.
        """
        from .gitea import GiteaApp
        from .vaultwarden_k8s_sync import VaultwardenK8sSyncApp
        from provisioner.lib.vaultwarden import VaultwardenClient

        # 1. Read the operator's .env for VW creds.
        env = VaultwardenK8sSyncApp._load_dotenv(ctx.repo_root)
        creds = GiteaApp._read_dotenv_creds(ctx.repo_root, catalog)
        if not creds["master_password"]:
            raise RuntimeError(
                "VAULTWARDEN__MASTERPASSWORD missing from .env; "
                "the orchestrator's contract is that Vaultwarden "
                "must be working before any app can install. Add "
                "VAULTWARDEN__MASTERPASSWORD=<pw> to "
                f"{ctx.repo_root / '.env'} (gitignored, mode 0600 "
                "is the orchestrator's convention). See "
                "docs/runbooks/setup-vaultwarden-sync.md."
            )

        # 2. Log in.
        client = VaultwardenClient.login(
            server_url=creds["server_url"],
            client_id=env.get("BW_CLIENTID", ""),
            client_secret=env.get("BW_CLIENTSECRET", ""),
            email=creds["email"],
            master_password=creds["master_password"],
        )
        creds["master_password"] = ""  # best-effort overwrite

        # 3. Find the cipher with VKS triple
        #    namespaces=gitea, secret-name=gitea-admin-password,
        #    secret-key=password.
        target_triple = {
            "namespaces": "gitea",
            "secret-name": "gitea-admin-password",
            "secret-key": "password",
        }
        existing = client.list_ciphers()
        for c in existing:
            triple: dict[str, str] = {}
            for i in range(len(c.get("fields") or [])):
                try:
                    k = client.decrypt_cipher_field_name(c, index=i)
                    triple[k] = client.decrypt_cipher_field(c, name=k)
                except Exception:
                    continue
            if all(triple.get(k) == v for k, v in target_triple.items()):
                try:
                    body = client.decrypt_cipher_notes(c)
                except Exception as e:
                    raise RuntimeError(
                        f"Vaultwarden returned a cipher matching "
                        f"the gitea-admin-pw triple but failed to "
                        f"decrypt its body: {type(e).__name__}: {e}. "
                        f"cipher id: {c.get('id')}. The cipher "
                        f"may have been tampered with — re-seed "
                        f"the admin password via the Vaultwarden "
                        f"web UI and re-apply."
                    ) from e
                if not body:
                    return None
                # Attach the authenticated client so the
                # caller can reuse it without a second
                # /identity/connect/token round-trip.
                return ("gitea_admin", body, client)
        return None

    def _mint_runner_token_from_gitea_api(
        self,
        ctx: Container,
        gitea_url: str,
        admin_username: str,
        admin_password: str,
    ) -> str:
        """Call the Gitea admin API

            GET {gitea_url}/api/v1/admin/runners/registration-token

        with HTTP Basic auth as the gitea_admin user, parse
        the JSON response, and return the token field. We
        use urllib directly (not the lib's HTTP client)
        because the lib's auth flow is for Vaultwarden
        and the Gitea admin API just wants straight
        Basic auth.
        """
        endpoint = f"{gitea_url}/api/v1/admin/runners/registration-token"
        token_pw = f"{admin_username}:{admin_password}"
        b64 = base64.b64encode(token_pw.encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            endpoint,
            headers={
                "Authorization": f"Basic {b64}",
                "Accept": "application/json",
                "User-Agent": "proxmox-cicd-orchestrator/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                ctx.logger.info(
                    "gitea_runner.token_minted",
                    endpoint=endpoint,
                    status_code=resp.status,
                    response_bytes=len(raw),
                )
        except urllib.error.HTTPError as e:
            # 401/403 most likely means the admin pw is
            # wrong OR VKS hasn't yet reconciled the new
            # Vaultwarden cipher back to the in-cluster
            # Secret. Surface a clear error.
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Gitea admin API rejected the run: "
                f"GET {endpoint} -> HTTP {e.code}. "
                f"Response body: {body[:300]}. "
                f"Most likely the gitea_admin password in "
                f"Vaultwarden hasn't been reconciled to the "
                f"in-cluster gitea-admin-password Secret by "
                f"VKS yet — wait up to 30s and re-apply. If "
                f"the problem persists, re-apply `gitea` to "
                f"refresh the cipher."
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"cannot reach Gitea admin API: GET {endpoint} "
                f"failed with {e!r}. Check that the gitea "
                f"chart has finished first-boot and the "
                f"Gateway/HTTPRoute is serving HTTPS."
            ) from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Gitea admin API returned non-JSON: "
                f"{raw[:300]!r}. The endpoint URL or auth "
                f"may be wrong."
            ) from e
        token = parsed.get("token", "")
        if not isinstance(token, str):
            return ""
        return token

    # `_kubectl` is inherited from `BaseApp` (WP6).


# Side-effect import: register on import.
register(GiteaRunnerApp)


__all__ = [
    "GiteaRunnerApp",
    "RUNNER_CONFIG_SECRET",
]
