"""Tests for the vaultwarden-k8s-sync (VKS) app.

Focus: the .env credential parser, since that's the seam
between the operator's local config and the auth Secret
the apply seeds into the cluster. The rest of apply() is
covered indirectly by the orchestrator + gitea-runner
test patterns (helm/kubectl mocking).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from provisioner.lib.apps.vaultwarden_k8s_sync import (
    APP_VERSION,
    CHART,
    CHART_VERSION,
    NAMESPACE,
    RELEASE,
    VaultwardenK8sSyncApp,
)
from provisioner.lib.apps import app_by_name, reset_registry
from provisioner.lib.container import Container


def _make_ctx(repo: Path) -> Container:
    return Container.for_tests(
        proxmox_k3s_repo=repo,
        repo_root=repo,
        audit_log=repo / "logs" / "test.audit.jsonl",
    )


# ----------------------------------------------------------- registry


def test_vaultwarden_k8s_sync_is_registered() -> None:
    import importlib

    from provisioner.lib.apps import vaultwarden_k8s_sync as vks_mod

    reset_registry()
    importlib.reload(vks_mod)
    assert app_by_name("vaultwarden-k8s-sync") is vks_mod.VaultwardenK8sSyncApp


# ----------------------------------------------------------- .env parser


def test_load_dotenv_missing(tmp_path: Path) -> None:
    """No .env file => all empty."""
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds == {
        "BW_CLIENTID": "",
        "BW_CLIENTSECRET": "",
        "VAULTWARDEN__MASTERPASSWORD": "",
        "VAULTWARDEN__SERVERURL": "",
    }


def test_load_dotenv_uppercase_bw_keys(tmp_path: Path) -> None:
    """Canonical key names (BW_CLIENTID / BW_CLIENTSECRET)."""
    (tmp_path / ".env").write_text(
        "BW_CLIENTID=user.abc\n"
        "BW_CLIENTSECRET=secret\n"
        "VAULTWARDEN__MASTERPASSWORD=mp\n"
        "VAULTWARDEN__SERVERURL=https://bitwarden.example.net\n"
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds == {
        "BW_CLIENTID": "user.abc",
        "BW_CLIENTSECRET": "secret",
        "VAULTWARDEN__MASTERPASSWORD": "mp",
        "VAULTWARDEN__SERVERURL": "https://bitwarden.example.net",
    }


def test_load_dotenv_lowercase_aliases(tmp_path: Path) -> None:
    """Bitwarden-web-UI-style names (client_id / client_secret)."""
    (tmp_path / ".env").write_text(
        "client_id=user.abc\n"
        "client_secret=secret\n"
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds["BW_CLIENTID"] == "user.abc"
    assert creds["BW_CLIENTSECRET"] == "secret"
    # master password still empty.
    assert creds["VAULTWARDEN__MASTERPASSWORD"] == ""


def test_load_dotenv_master_aliases(tmp_path: Path) -> None:
    """VAULTWARDEN_MASTERPASSWORD + master_password are aliases."""
    (tmp_path / ".env").write_text(
        "BW_CLIENTID=user.abc\n"
        "BW_CLIENTSECRET=secret\n"
        "VAULTWARDEN_MASTERPASSWORD=mp1\n"
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds["VAULTWARDEN__MASTERPASSWORD"] == "mp1"

    (tmp_path / ".env").write_text(
        "BW_CLIENTID=user.abc\n"
        "BW_CLIENTSECRET=secret\n"
        "master_password=mp2\n"
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds["VAULTWARDEN__MASTERPASSWORD"] == "mp2"


def test_load_dotenv_serverurl_aliases(tmp_path: Path) -> None:
    """VAULTWARDEN_URL / BITWARDEN_URL are SERVERURL aliases."""
    for alias in (
        "VAULTWARDEN_URL",
        "BITWARDEN_URL",
        "VAULTWARDEN_SERVERURL",
    ):
        (tmp_path / ".env").write_text(
            f"{alias}=https://bitwarden.example.net\n"
        )
        creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
        assert creds["VAULTWARDEN__SERVERURL"] == (
            "https://bitwarden.example.net"
        ), f"alias {alias} should map to VAULTWARDEN__SERVERURL"


def test_load_dotenv_handles_quotes_and_spaces(tmp_path: Path) -> None:
    """Value side: trim leading/trailing whitespace + quotes."""
    (tmp_path / ".env").write_text(
        "client_id=   user.abc  \n"
        'client_secret="secret"\n'
        "client_secret_strict='secret2'\n"  # unknown key -> ignored
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds["BW_CLIENTID"] == "user.abc"
    assert creds["BW_CLIENTSECRET"] == "secret"


def test_load_dotenv_skips_comments_and_blanks(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "BW_CLIENTID=user.abc\n"
        "# another comment\n"
        "BW_CLIENTSECRET=secret\n"
    )
    creds = VaultwardenK8sSyncApp._load_dotenv(tmp_path)
    assert creds["BW_CLIENTID"] == "user.abc"
    assert creds["BW_CLIENTSECRET"] == "secret"


# ----------------------------------------------------------- _render_values


def test_render_values_unchanged_without_url(tmp_path: Path) -> None:
    """No server_url => returns the committed file untouched."""
    values = tmp_path / "vaultwarden-kubernetes-secrets.yaml"
    values.write_text(
        "env:\n  config:\n    VAULTWARDEN__SERVERURL: "
        '"https://bitwarden.example.net"\n'
    )
    out = VaultwardenK8sSyncApp._render_values(values, "")
    assert out == values
    assert not (tmp_path / "vaultwarden-kubernetes-secrets.values-rendered.yaml").exists()


def test_render_values_overlays_url(tmp_path: Path) -> None:
    """server_url is set => rendered file has the URL."""
    values = tmp_path / "vaultwarden-kubernetes-secrets.yaml"
    values.write_text(
        "env:\n  config:\n"
        '    VAULTWARDEN__SERVERURL: "https://bitwarden.example.net"\n'
        "    OTHER: keep\n"
    )
    out = VaultwardenK8sSyncApp._render_values(
        values, "https://vault.example.com"
    )
    assert out != values
    text = out.read_text()
    assert '"https://vault.example.com"' in text
    # The non-overridden line is preserved.
    assert "OTHER: keep" in text
    # Clean up so the rendered file doesn't leak
    # into the cwd.
    out.unlink()


# ----------------------------------------------------------- plan


def test_vaultwarden_k8s_sync_plan_mentions_oci_chart(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    plan = VaultwardenK8sSyncApp().plan(ctx, {})
    assert plan.app_name == "vaultwarden-k8s-sync"
    assert any(
        "oci://ghcr.io/antoniolago/charts/vaultwarden-kubernetes-secrets" in s
        for s in plan.would_install
    )
    assert any("--version 2.0.0" in s for s in plan.would_install)
    assert any(
        f"-n {NAMESPACE}" in s for s in plan.would_install
    )
    assert any("ephemeral" not in n for n in plan.notes)
    # post-install note should mention credential seed.
    assert any(
        "BW_CLIENTID" in n
        or "VAULTWARDEN__MASTERPASSWORD" in n
        for n in plan.notes
    )


# ----------------------------------------------------------- apply (smoke)


def test_vaultwarden_k8s_sync_apply_seeds_secret_from_dotenv(tmp_path: Path) -> None:
    """Apply must kubectl.apply an Opaque Secret with the BW_*
    keys populated from .env. Master password is NOT in
    .env -> must NOT be emitted (so a re-apply doesn't
    clobber a value the operator set out-of-band).
    """
    repo = tmp_path
    (repo / ".env").write_text(
        "client_id=user.abc\n"
        "client_secret=secret\n"
    )
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    (k8s / "kubeconfig.yaml").write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- cluster:\n    server: https://10.0.0.64:6443\n"
        "  name: cicd\n"
        "contexts:\n- context:\n    cluster: cicd\n    user: cicd\n"
        "  name: cicd\n"
        "current-context: cicd\n"
        "users:\n- name: cicd\n  user:\n    token: t\n"
    )
    (repo / "values").mkdir(parents=True, exist_ok=True)
    (repo / "values" / "vaultwarden-kubernetes-secrets.yaml").write_text(
        "# test values\nnamespace: { name: vaultwarden-kubernetes-secrets }\n"
    )

    ctx = _make_ctx(repo)
    helm_mock = MagicMock()
    helm_mock.install_or_upgrade = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    helm_mock.uninstall = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    helm_mock.list_releases = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.helm = helm_mock

    kubectl_mock = MagicMock()
    kubectl_mock.apply = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    kubectl_mock.wait_deployments_available = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    kubectl_mock.delete_namespace = MagicMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )
    ctx.kubectl = kubectl_mock

    result = VaultwardenK8sSyncApp().apply(ctx, {})

    # 1. helm install ran with --wait=false.
    helm_args, helm_kwargs = helm_mock.install_or_upgrade.call_args
    assert helm_kwargs["chart"] == CHART
    assert helm_kwargs["version"] == CHART_VERSION
    assert helm_kwargs["namespace"] == NAMESPACE
    assert helm_kwargs["release"] == RELEASE
    assert helm_kwargs["extra_args"] == ("--wait=false",)

    # 2. kubectl apply was called at least once with the
    #    auth Secret. The manifest must contain the two
    #    keys .env had, and must NOT contain an empty
    #    VAULTWARDEN__MASTERPASSWORD (which would
    #    clobber the live value).
    apply_calls = kubectl_mock.apply.call_args_list
    assert apply_calls, "expected at least one kubectl.apply call"
    secret_apply = next(
        c for c in apply_calls
        if "BW_CLIENTID" in str(c)
    )
    secret_manifest = secret_apply.kwargs.get("manifest") or secret_apply.args[0]
    assert "BW_CLIENTID: user.abc" in secret_manifest
    assert "BW_CLIENTSECRET: secret" in secret_manifest
    # Master password absent from .env -> not in the
    # manifest. This is the regression guard: a previous
    # version emitted `VAULTWARDEN__MASTERPASSWORD: ` as
    # an empty value, which clobbered the operator's
    # out-of-band value.
    assert "VAULTWARDEN__MASTERPASSWORD" not in secret_manifest
    assert result.app_name == "vaultwarden-k8s-sync"
    assert result.namespace == NAMESPACE
    assert result.next_step is not None
    assert "VAULTWARDEN__MASTERPASSWORD" in result.next_step


def test_vaultwarden_k8s_sync_apply_emits_auto_step_when_fully_seeded(
    tmp_path: Path,
) -> None:
    """When .env has all three keys, the next-step is
    'credentials auto-seeded' rather than the manual
    fallback.
    """
    repo = tmp_path
    (repo / ".env").write_text(
        "client_id=user.abc\n"
        "client_secret=secret\n"
        "master_password=mp\n"
    )
    k8s = repo / "infra" / "clusters" / "cicd"
    k8s.mkdir(parents=True)
    (k8s / "kubeconfig.yaml").write_text(
        "apiVersion: v1\nkind: Config\nclusters: [{}]\n"
        "contexts: [{}]\ncurrent-context: x\nusers: [{}]\n"
    )
    (repo / "values").mkdir(parents=True, exist_ok=True)
    (repo / "values" / "vaultwarden-kubernetes-secrets.yaml").write_text("# ok\n")

    ctx = _make_ctx(repo)
    ctx.helm = MagicMock(
        install_or_upgrade=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
        uninstall=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
        list_releases=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
    )
    ctx.kubectl = MagicMock(
        apply=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
        wait_deployments_available=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
        delete_namespace=MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
    )

    result = VaultwardenK8sSyncApp().apply(ctx, {})
    assert result.next_step is not None
    assert "auto-seeded" in result.next_step.lower()
    # Master password must land in the Secret.
    apply_calls = ctx.kubectl.apply.call_args_list
    secret_apply = next(c for c in apply_calls if "BW_CLIENTID" in str(c))
    secret_manifest = secret_apply.kwargs.get("manifest") or secret_apply.args[0]
    assert "VAULTWARDEN__MASTERPASSWORD: mp" in secret_manifest


# ----------------------------------------------------------- constants


def test_vaultwarden_k8s_sync_chart_constants() -> None:
    assert CHART == "oci://ghcr.io/antoniolago/charts/vaultwarden-kubernetes-secrets"
    assert CHART_VERSION == "2.0.0"
    assert APP_VERSION == "2.0.0"
    assert NAMESPACE == "vaultwarden-kubernetes-secrets"
    assert RELEASE == "vaultwarden-kubernetes-secrets"
