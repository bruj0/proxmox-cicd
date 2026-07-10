# Runbook: add a 4th app to the catalog

The catalog at v0.1.0 ships gitea, gitea-runner, and
bitwarden-sm-operator. To add `harbor` (image registry) or
`woodpecker` (alternative CI runner) tomorrow:

## 5-step recipe

### 1. Pin the version

Edit `versions.yaml` and `versions.lock.yaml`. Add a new
top-level key (`harbor:`) with the chart and image pins +
source URLs + fetched date. Mirror the structure of the
existing entries.

### 2. Render the values

Create `values/harbor.yaml` with the chart overrides. Read
the chart's `values.yaml` first to understand what
production-grade defaults you need to override (resources,
persistence, ingress).

Persistence MUST point at `proxmox-lvm-thin` (stage 2's
StorageClass). No hostPaths, no EmptyDir for stateful data.

Ingress MUST go through Gateway API (the chart's own
`ingress:` block is Ingress-NGINX-shaped; disable it and
apply your own `Gateway` + `HTTPRoute` via
`KubectlRunner.apply`, like the gitea app does).

### 3. Write the AppSpec

Create `provisioner/lib/apps/harbor.py`. Minimum:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provisioner.lib.container import Container
from provisioner.lib.kubectl_runner import KubectlRunner
from provisioner.lib.apps import (
    AppApplyResult, AppPlanResult, AppSpec, AppStatus, register
)

CHART = "oci://goharbor/harbor-helm"
CHART_VERSION = "1.13.0"
NAMESPACE = "harbor"
RELEASE = "harbor"
DEFAULT_VALUES_FILE = "values/harbor.yaml"

@dataclass
class HarborApp:
    name: str = "harbor"

    def _values_file(self, ctx: Container) -> Path:
        return ctx.repo_root / DEFAULT_VALUES_FILE

    def plan(self, ctx, catalog): ...
    def apply(self, ctx, catalog): ...
    def destroy(self, ctx, catalog): ...
    def status(self, ctx, catalog): ...

register(HarborApp)
```

Copy the structure from `apps/gitea.py` and adapt. The
4-method contract is exactly the same.

### 4. Force-import in cli.py

In `provisioner/cli.py`, add:

```python
from .lib.apps import harbor as _harbor  # noqa: F401
```

This triggers the `@register` decorator at startup. Without
it, the app wouldn't appear in the registry.

### 5. Enable in catalog.yaml

In `infra/clusters/cicd/catalog.yaml`:

```yaml
apps:
  harbor:
    enabled: true
```

## Verify

```bash
make validate CLUSTER=cicd           # parses catalog
make plan     CLUSTER=cicd          # shows the plan
make apply    CLUSTER=cicd          # installs
make status   CLUSTER=cicd          # confirms install
pytest provisioner/tests/ -q        # all tests still pass
```

The orchestrator source is unchanged. The planner picks up
the new app automatically. The only test that changes is
`tests/test_orchestrator.py::test_orchestrator_does_not_
import_app_specific_symbols` — add `harbor` to the forbidden
list there too.

## Why this works

- **S** — HarborApp lives in one file.
- **O** — orchestrator.py is unchanged.
- **L** — HarborApp implements the same 4-method contract.
- **I** — HarborApp's protocol surface is identical.
- **D** — HarborApp takes a Container, not concrete runners.

The `test_orchestrator_does_not_import_app_specific_
symbols` test is the safety net. If someone refactors
orchestrator.py to import from `apps.harbor`, the test
fires and the PR is blocked.