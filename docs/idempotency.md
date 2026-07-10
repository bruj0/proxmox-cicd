# Idempotency

> What `make apply` does on every re-run against a
> healthy cluster.

## The TL;DR

`make apply CLUSTER=cicd` is safe to re-run any number of
times. After the first successful apply, subsequent runs
are no-ops on the cluster side; the orchestrator's only
output is the `apps.json` handoff getting refreshed.

## The two primitives

Every mutation in the orchestrator goes through one of two
idempotency-safe primitives:

1. **`helm upgrade --install`** (in `HelmRunner.install_or_upgrade`).
   Helm reports the diff: `deployed` if nothing changed,
   `upgraded` if values changed.
2. **`kubectl apply --server-side`** (in `KubectlRunner.apply`).
   Server-side apply is deterministic: the apiserver applies
   the last-write-wins patch and reports `unchanged`.

We **never** run `helm install` or `helm upgrade` separately
(the `--install` flag is on by default in our runner).
We **never** run `kubectl apply` without `--server-side`.

## The two-writer rule

`apps.json` has exactly one writer:
`provisioner/lib/output_writer.py::write_apps_json`. The
orchestrator calls it exactly once at the end of a successful
apply. Helm + kubectl never write to it. Stage 2's
`output.json` has its own equivalent rule.

## Namespace cleanup

When an app's `.destroy()` deletes its namespace, k8s
starts the Terminating process. The PVC's
`kubernetes.io/pvc-protection` finalizer keeps the namespace
alive while k8s tears down the volume. Cleanup is done
with `--wait=true --timeout=60s`, so the orchestrator only
proceeds to the next phase once the namespace is fully gone.

## What `make apply` does on a steady-state cluster

```text
$ make apply CLUSTER=cicd PROXMOX_K3S_REPO=../proxmox-k3s SSH_KEY=~/.ssh/id_rsa
[INFO] env_loaded
[INFO] phase_start
[INFO] phase_start   (phase: bitwarden-sm-operator)
[INFO] helm_repo_add (repo: bitwarden, url: https://charts.bitwarden.com/)
[INFO] helm_repo_update
[INFO] bitwarden_sm.helm_install_ok
[INFO] phase_done
[INFO] phase_start   (phase: gitea)
[INFO] gitea.helm_install_ok
[INFO] gitea.gateway_applied
[INFO] phase_done
[INFO] phase_start   (phase: gitea-runner)
[INFO] gitea_runner.no_values_file
[INFO] gitea_runner.bitwardensecret_applied
[INFO] gitea_runner.helm_install_ok
[INFO] phase_done
apply complete: 3 apps installed
```

If the cluster is already healthy:

- `helm upgrade --install` reports `STATUS: deployed`
  (no change).
- `kubectl apply --server-side` reports `unchanged`.
- `apps.json` is rewritten with a fresh `applied_at`
  timestamp.

## Failure modes

### Helm install fails mid-chart

If `helm upgrade --install` returns rc != 0, the
orchestrator raises `RuntimeError` and stops. The partial
state is on the cluster (some resources were created, some
weren't) but re-running `make apply` will retry the
install until it converges. Helm is idempotent on its own
resources.

### `kubectl apply` hits "namespace is being terminated"

The orchestrator's cleanup uses `--wait=true --timeout=60s`,
so this race is rare. If it happens anyway, the next
`make apply` will see the namespace as Terminating and
fail with a clear error; just wait 60s and re-run.

### An app's PVC stays Pending forever

If `proxmox-csi-plugin` is misconfigured, a PVC will sit
Pending and the app's pods will CrashLoopBackOff. The
orchestrator's `_wait_data_plane_ready` will time out and
the apply will fail with a clear error. Fix the CSI
plugin, then re-run.

### BitwardenSecret stuck NotSynced

If the BitwardenSecrets Manager sync fails (wrong org ID,
revoked token, network partition), the `BitwardenSecret` CR
will stay in `NotSynced` status. The orchestrator doesn't
block on this; the next apply will retry the CR apply. Check
the `sm-operator` controller logs in
`sm-operator-system`.

## What changes on a re-run

The **only** thing that changes on a re-run is
`apps.json`'s `applied_at` timestamp. Everything else is
helm+kubectl server-side convergence.