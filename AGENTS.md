# AGENTS.md — guide for AI agents modifying this repo

This repository deploys an extensible catalog of operator-facing
applications on top of a k3s cluster. It is the third (and
final) stage of a three-stage provisioning pipeline:

```
proxmox-vms (stage 1) -> proxmox-k3s (stage 2) -> proxmox-cicd (stage 3)
```

The codebase is the landing of a 16-work-package plan that
took it from "AppSpec Protocol + ad-hoc helper duplication"
to "BaseApp ABC + shipped catalog + groups + render layer".
The plan lives at
[`docs/plans/2026-07-15-sequence-abstraction-plan.md`](docs/plans/2026-07-15-sequence-abstraction-plan.md)
and is the design rationale for every file you open here.

## Read first

1. **`README.md`** — operator-facing entry point.
2. **`docs/architecture.md`** — subsystem boundaries and the SOLID seams (BaseApp / Container / planner / groups).
3. **`docs/plans/2026-07-15-sequence-abstraction-plan.md`** — the WP0–WP15 plan; each work package has a landed marker + implementation note.
4. **`docs/idempotency.md`** — what `make apply` does on every re-run.

## Repository conventions

### File layout

- `provisioner/` — Python orchestrator (stdlib only; ruff + mypy --strict).
  - `provisioner/cli.py` — `cicdctl plan|apply|destroy|status|validate|render`.
  - `provisioner/lib/` — internal helpers (DI, log, catalog, planner, runner).
  - `provisioner/lib/apps/` — one file per `BaseApp` subclass.
    `base.py` is the canonical ABC every app inherits.
  - `provisioner/lib/groups/` — group-aware orchestration
    (WP2): `DefaultGroup`, `CicdStackGroup`, `BaseGroup` ABC,
    `resolve_apply_order(...)` topological sorter.
  - `provisioner/lib/catalog/shipped.yaml` — the version
    contract: every app this version of `proxmox-cicd`
    knows how to install (WP1).
  - `provisioner/lib/render_values.py` — WP10 single source of
    truth for "what gets sent to helm"; reached for via
    `BaseApp._render_for_apply(...)` and `cicdctl render`.
  - `provisioner/tests/` — pytest suite; no live cluster required.
- `infra/charts/gitea-runner/` — the ONE chart we own.
- `infra/helm-charts/cloudflare-tunnel-remote-0.1.2.tgz` —
  vendored remote-managed chart (cloudflared).
- `infra/clusters/<name>/catalog.yaml` — operator-edited.
- `infra/clusters/<name>/apps.json` — generated handoff (gitignored).
- `values/<app>.yaml` — helm values overrides (one file per app;
  the WP10 file-move to `infra/clusters/<name>/values/` is
  deferred).
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

- **S** — every `BaseApp` subclass is one file:
  `provisioner/lib/apps/<name>.py`. The 4-method contract
  (`plan` / `apply` / `destroy` / `status`) lives on
  `BaseApp` (WP0 / WP15).
- **O** — adding an app is one file + one import in `cli.py`
  to force-register it + one entry in `provisioner/lib/catalog/shipped.yaml`
  to declare its chart / image / namespace / release. The
  orchestrator + planner + CLI are unchanged. The
  `test_orchestrator_does_not_import_app_specific_symbols`
  test pins this property: grep the orchestrator source
  for `from .apps.gitea` etc. — those should be absent.
- **L** — every `BaseApp` subclass honors the same 4-method
  contract. `BaseApp._rendered_values_file` /
  `BaseApp._values_file` / `BaseApp._kubectl` /
  `BaseApp._vaultwarden_client` /
  `BaseApp._seed_vaultwarden_note` /
  `BaseApp._read_dotenv_creds` are the shared seam; apps
  subclass behaviour without rewriting the contract.
- **I** — apps reach for the canonical helpers on `BaseApp`
  rather than re-implementing. The static guards
  (`tests/test_apps_no_inline_wp9_patterns.py`,
  `tests/test_apps_no_inline_vaultwarden_client.py`,
  `tests/test_no_alt_render_layer.py`,
  `tests/test_apps_no_appspec_refs.py`) fail the build
  when a future contributor reintroduces an inline pattern.
- **D** — apps depend on `Container`, not concrete runners.

### Canonical helpers on `BaseApp` (WP6 / WP9 / WP11 / WP12)

Apps reach for these rather than re-implementing:

| Helper | Purpose | Introduced |
|---|---|---|
| `_kubectl(ctx)` | Resolve the per-cluster kubeconfig + runner | WP6 |
| `_values_file(ctx)` | Path to the per-app committed values YAML | WP9 |
| `_rendered_values_file(ctx)` | Path to the per-apply *rendered* values YAML (sibling of `_values_file`) | WP9 |
| `_secret_ref(name, key)` | Build a `SecretKeySelector` block | WP9 |
| `_hostname(...)`, `_labels(...)`, `_annotations(...)` | Common helpers | WP9 |
| `_render_template(name, **vars)` | `string.Template` substitution for embedded YAML | WP5 |
| `_parse_dotenv(text)` / `_load_dotenv(repo_root)` / `_require_env(env, key)` | `.env` parser (no per-app copies) | WP11 |
| `_read_dotenv_creds(repo_root, catalog)` | Vaultwarden creds precedence (catalog > .env > canonical) | WP12 |
| `_vaultwarden_client(ctx, catalog)` | Reads BW_CLIENTID + BW_CLIENTSECRET, decodes, logs in | WP12 |
| `_seed_vaultwarden_note(ctx, client, catalog, *, note_name, body_text, namespace, secret_name, secret_key, app_short=...)` | Idempotent VKS-triple cipher push | WP12 |
| `_render_for_apply(ctx, cluster_name, catalog)` | WP10 deep-merge + write to `.proxmox-cicd/rendered/<cluster>/<app>.yaml` | WP10 |

Class attrs every app declares (WP13):

```python
class GiteaApp(BaseApp):
    name = "gitea"                                          # ClassVar[str]
    namespace: ClassVar[str] = "gitea"
    release: ClassVar[str] = "gitea"
    chart: ClassVar[str] = "oci://docker.gitea.com/charts/gitea"
    chart_version: ClassVar[str] = "12.0.0"
    image_version: ClassVar[str] = "1.26.x"
    default_values_file: ClassVar[str] = "values/gitea.values-rendered.yaml"
    _rendered_values_filename: ClassVar[str | None] = "cloudflare-tunnel-remote.values-rendered.yaml"  # only for vendored charts
```

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

### Render layer (WP10)

`cicdctl render cicd [--app NAME]` is a read-only operator
tool that deep-merges each app's shipped defaults + per-cluster
overlay and writes the result to
`.proxmox-cicd/rendered/<cluster>/<app>.yaml`. It's the
single source of truth for "what would `apply` send to helm".
The CLI exits 0 (success), 3 (catalog parse failed), or 9
(render failed — e.g. an app has no shipped defaults AND no
per-cluster overlay; the helper raises `NoShippedDefaultsError`).

### Version pinning

Every chart and image reference has a pin in `versions.yaml`
and a corresponding `ClassVar[str]` in the relevant
`apps/<name>.py` (WP13 — module-level constants were deleted).
Operators bump both in lockstep.

### Security

- **Never commit `.env`, `terraform.tfvars`, `output.json`,
  `*.tfstate*`, or `apps.json`** (all gitignored).
- Vaultwarden creds are read from `.env` at apply-time via
  `BaseApp._read_dotenv_creds(...)` (WP12 — the
  `VAULTWARDEN__MASTERPASSWORD` key). The VKS in-cluster
  Secret is read for `BW_CLIENTID` / `BW_CLIENTSECRET` via
  `BaseApp._vaultwarden_client(ctx, catalog)`.
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

The four static guards (WP9 / WP10 / WP12 / WP15) ship with
the codebase:

- `tests/test_apps_no_inline_wp9_patterns.py` —
  `SecretKeySelector`, hardcoded `chart_version`, inline
  `values-rendered` filename logic.
- `tests/test_no_alt_render_layer.py` —
  `yaml.safe_dump` / `yaml.dump` / `yaml.safe_load` in
  `apps/*.py` (the WP10 render layer is the only allowed
  writer).
- `tests/test_apps_no_inline_vaultwarden_client.py` —
  `VaultwardenClient` direct imports in `apps/*.py` (the
  WP12 helper is the only allowed consumer).
- `tests/test_apps_no_appspec_refs.py` — `AppSpec` references
  in `apps/*.py` (WP15 — the alias lives only in
  `apps/__init__.py`).

## Adding a 5th app

See `docs/runbooks/add-an-app.md`. The recipe now has three
required touch points (was two pre-WP1):

1. Create `provisioner/lib/apps/<new>.py` with a `BaseApp`
   subclass that declares `name` / `namespace` / `release`
   / `chart` / `chart_version` / `image_version` as
   `ClassVar[str]`s and implements the 4-method contract.
2. Add the new app to `provisioner/lib/catalog/shipped.yaml`
   (version contract: chart, namespace, release, defaults).
3. Add `from .lib.apps import <new> as _<new>` to `cli.py`
   so `@register` runs at CLI startup.
4. Add `<new>: { enabled: true }` to
   `infra/clusters/<name>/catalog.yaml`.