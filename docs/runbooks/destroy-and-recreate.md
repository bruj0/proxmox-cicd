# Runbook: destroy and recreate the app catalog

When you want to start over — for example, after a major
chart upgrade or a destructive test — the `cicdctl destroy`
+ `cicdctl apply` cycle is idempotent. PVCs are NOT
preserved by default; see below.

## Standard cycle

```bash
# 1. Confirm what will be destroyed.
make status CLUSTER=cicd

# 2. Destroy.
make destroy CLUSTER=cicd PROXMOX_K3S_REPO=../proxmox-k3s SSH_KEY=~/.ssh/id_rsa

# 3. Verify clean.
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  get ns | grep -E 'gitea|bitwarden|sm-operator'
# -> no matches (or only Terminating ones; wait 60s)

# 4. Recreate.
make apply CLUSTER=cicd PROXMOX_K3S_REPO=../proxmox-k3s SSH_KEY=~/.ssh/id_rsa
```

`make destroy` runs each app's `.destroy()` in **reverse
registration order**, so dependents (gitea-runner) are
uninstalled before their dependencies (gitea). For each
app:

1. `helm uninstall <release> -n <ns>` (deletes the
   Deployment + Service + RBAC).
2. `kubectl delete ns <ns> --wait=true --timeout=60s`
   (deletes everything else in the namespace, including
   the BitwardenSecret CR and any leftover PVCs).

If a PVC's `kubernetes.io/pvc-protection` finalizer blocks
deletion, you'll see `Terminating` in `kubectl get ns`. The
PVC will go away after the proxmox-csi-plugin finishes its
CSI delete workflow (~30s).

## Preserving data across destroy/apply

If you want to keep the Gitea repo data across cycles
(useful for testing an upgrade path), do NOT include
`gitea` in `make destroy`. Instead:

```bash
# Just destroy the apps you want to rebuild.
cicdctl --proxmox-k3s-repo ../proxmox-k3s destroy cicd --auto-approve \
  && kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
       apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: gitea-runner
EOF
make apply CLUSTER=cicd
```

(The `cicdctl destroy` target doesn't currently take a
`--only` flag; this workaround manually recreates the
namespace so the next `make apply` succeeds.)

For the v0.1.0 catalog, every PVC has
`helm.sh/resource-policy: keep` set in `values/gitea.yaml`,
so a `helm uninstall` does NOT delete the PVC. This means
`make destroy` + `make apply` preserves Gitea's data even
without the workaround above.

## When the k3s cluster is in a bad state

If the k3s apiserver itself is misbehaving, `cicdctl
destroy` will hang on `kubectl delete ns --wait=true`. You
have two options:

### 1. From-the-cluster-side destroy

SSH to each VM and `k3s kubectl delete ns gitea gitea-runner
sm-operator-system` directly. The cleanup logic is the
same; you're just bypassing the SSH-via-apiserver round
trip.

### 2. Bypass the namespace-deletion wait

```bash
# This destroys the apps but leaves the namespaces
# in Terminating. You can clean them up later.
for ns in gitea gitea-runner sm-operator-system; do
  kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
    delete ns "$ns" --wait=false || true
done
```

## Verifying a fresh install

After `make apply`, the orchestrator should report
`apply complete: 3 apps installed`. Confirm:

```bash
make status CLUSTER=cicd
# app                     namespace              installed chart        image
# bitwarden-sm-operator   sm-operator-system     yes      0.4.0        0.4.0
# gitea                   gitea                  yes      12.0.0       1.26.x
# gitea-runner            gitea-runner           yes      0.2.0        1.0.8-dind
```

And on the cluster:

```bash
kubectl --kubeconfig ../proxmox-k3s/infra/clusters/cicd/kubeconfig.yaml \
  get pods -A | grep -E 'gitea|bitwarden|sm-operator'
# -> every pod Running or Completed
```