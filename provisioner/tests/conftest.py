"""conftest — fixtures shared across proxmox-cicd tests.

Mirrors proxmox-k3s/provisioner/tests/conftest.py in spirit
(small, in-memory fakes), but the fixtures are tailored to
the app-catalog use case: the unit under test is an
`AppSpec`, not a `Phase`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the `provisioner` package importable regardless of cwd.
PROVISIONER_DIR = Path(__file__).resolve().parent.parent
if str(PROVISIONER_DIR) not in sys.path:
    sys.path.insert(0, str(PROVISIONER_DIR))


@pytest.fixture
def fake_subprocess() -> MagicMock:
    """A MagicMock that quacks like `subprocess.run`.

    Tests configure it with `.return_value` for happy-path
    checks and `.side_effect` for multi-call flows. Every
    test asserts on `.call_args_list` to confirm the right
    subprocess command was built.
    """
    m = MagicMock()
    m.return_value = MagicMock(returncode=0, stdout="", stderr="")
    return m


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Iterator[Path]:
    """Yield a tmp directory shaped like a proxmox-cicd repo.

    The directory has infra/clusters/<cluster>/ and values/
    laid out exactly as the real repo. Tests write catalog.yaml
    + kubeconfig.yaml + apps.json into it.
    """
    repo = tmp_path
    (repo / "infra" / "clusters" / "cicd").mkdir(parents=True)
    (repo / "values").mkdir(parents=True)
    (repo / "logs").mkdir(parents=True)
    yield repo
