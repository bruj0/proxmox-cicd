# AGENTS.md — guide for AI agents modifying this repo

This repository deploys an extensible catalog of operator-facing
applications on top of a k3s cluster. It is the third (and
final) stage of a three-stage provisioning pipeline:

```
proxmox-vms (stage 1) -> proxmox-k3s (stage 2) -> proxmox-cicd (stage 3)
```

## Read first

1. **`README.md`** — operator-facing entry point.
2. **`docs/PLAN.md`** — design rationale, work packages, open questions.
3. **`docs/architecture.md`** — subsystem boundaries and the SOLID seams.
4. **`docs/idempotency.md`** — what `make apply` does on every re-run.

## Repository conventions

### File layout

- `provisioner/` — Python orchestrator (stdlib only; ruff + mypy --strict).
  - `provisioner/cli.py` — `cicdctl plan|apply|destroy|status|validate`.
  - `provisioner/lib/` — internal helpers (DI, log, catalog, planner, runner).
  - `provisioner/lib/apps/` — one file per AppSpec implementation.
  - `provisioner/tests/` — pytest suite; no live cluster required.
- `infra/charts/gitea-runner/` — the ONE chart we own.
- `infra/clusters/<name>/catalog.yaml` — operator-edited.
- `infra/clusters/<name>/apps.json` — generated handoff (gitignored).
- `values/<app>.yaml` — helm values overrides (one file per app).
- `versions.yaml` — master compatibility matrix.
- `versions.lock.yaml` — pinned versions for the orchestrator.

### Python style

- `make lint` must pass (ruff check + mypy --strict on
  `provisioner/lib/`).
- `make test` must pass (pytest, no live cluster required).
- All CLI entry points return `int` (the exit code).
- Secrets are redacted in the audit log by `StructuredLogger`
  (`provisioner/lib/log.py`); keys whose name contains
  `secret` / `token` / `password` / `ssh_key` are dropped
  recursively before any line is written.
- Use dataclasses for typed state (`AppApplyResult`, `Catalog`, etc.).
- No third-party deps. Stdlib only.

### SOLID principles

This is the most important architectural decision in this repo:

- **S** — every AppSpec is one file: `provisioner/lib/apps/<name>.py`.
- **O** — adding an app is one file + one entry in `cli.py`; the
  orchestrator + planner + CLI are unchanged.
- **L** — every AppSpec implements the same 4-method contract.
- **I** — the `AppSpec` protocol exposes only what the orchestrator needs.
- **D** — apps depend on `Container`, not concrete runners.

The `test_orchestrator_does_not_import_app_specific_symbols` test
pins the Open/Closed property: grep the orchestrator source
for `from .apps.gitea` etc. — those should be absent.

### Idempotency

Every apply is built around two primitives:

1. `helm upgrade --install` (in `HelmRunner.install_or_upgrade`)
2. `kubectl apply --server-side` (in `KubectlRunner.apply`)

Re-running `cicdctl apply cicd` against a healthy cluster is a
no-op (both commands return "unchanged"). There's no custom
state-tracking JSON for apps.

If `kubectl apply` hits `namespace is being terminated`, the
phase's cleanup waits with `--wait=true --timeout=60s` for the
namespace to fully terminate before exiting.

### Version pinning

Every chart and image reference has a pin in `versions.yaml`
and a corresponding constant in the relevant `apps/<name>.py`.
Operators bump both in lockstep.

### Security

- **Never commit `.env`, `terraform.tfvars`, `output.json`,
  `*.tfstate*`, or `apps.json`** (all gitignored).
- The Bitwarden access token is read from `.env` at apply-time
  and passed to the `BitwardenSecret` CR via the
  `bw-auth-token` Secret. After the first sync the secret
  lives in k8s; the orchestrator never reads it again.
- The apps.json handoff is mode 0600 (see
  `output_writer.write_apps_json`).
- The audit logger redacts `secret`, `token`, `password`,
  `ssh_key`, `sshkey` keys before writing to disk.

### Two-writer rule

`apps.json` is the canonical handoff. There's exactly one
writer: `output_writer.write_apps_json`, called by the
orchestrator after a successful apply. Helm + kubectl never
write to it directly.

## Tests

`make test` runs the entire pytest suite. Tests use
`unittest.mock.MagicMock` to substitute the HelmRunner +
KubectlRunner on the Container. No live cluster required.

The registry side-effect (every `@register`'d app) is reset
in `conftest.py`'s autouse fixture before each test.

## Adding a 4th app

See `docs/runbooks/add-an-app.md`.