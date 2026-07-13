"""cli — `cicdctl plan|apply|destroy|status|validate`.

Subcommands keep the surface small. Every subcommand takes a
positional <cluster> argument and reads its catalog from
`infra/clusters/<cluster>/catalog.yaml`. The kubeconfig is
read from the sibling proxmox-k3s repo's
`infra/clusters/<cluster>/kubeconfig.yaml`.

Exit codes:

   0  success
   2  prerequisite failure (kubectl/helm missing, kubeconfig missing)
   3  catalog parse failed
   4  plan failed
   5  apply failed
   6  destroy failed
   7  status failed
   8  validate failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROVISIONER_DIR = Path(__file__).resolve().parent
REPO_ROOT_DEFAULT = PROVISIONER_DIR.parent
if str(PROVISIONER_DIR) not in sys.path:
    sys.path.insert(0, str(PROVISIONER_DIR))


EXIT_OK = 0
EXIT_PREREQ = 2
EXIT_CATALOG = 3
EXIT_PLAN = 4
EXIT_APPLY = 5
EXIT_DESTROY = 6
EXIT_STATUS = 7
EXIT_VALIDATE = 8


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cicdctl",
        description="Deploy the proxmox-cicd app catalog onto a k3s cluster.",
    )
    parser.add_argument(
        "--proxmox-k3s-repo",
        type=Path,
        default=REPO_ROOT_DEFAULT.parent / "proxmox-k3s",
        help="Path to the sibling proxmox-k3s repo (default: ../proxmox-k3s).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cluster_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "cluster",
            help="Cluster name; maps to infra/clusters/<cluster>/.",
        )

    p_plan = sub.add_parser("plan", help="Diff desired vs live apps.")
    add_cluster_arg(p_plan)

    p_apply = sub.add_parser("apply", help="Install app catalog (idempotent).")
    add_cluster_arg(p_apply)
    p_apply.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    p_destroy = sub.add_parser("destroy", help="Uninstall every app.")
    add_cluster_arg(p_destroy)
    p_destroy.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    p_status = sub.add_parser("status", help="Show live app state.")
    add_cluster_arg(p_status)

    p_validate = sub.add_parser(
        "validate", help="Parse catalog + values; no kubectl/helm."
    )
    add_cluster_arg(p_validate)

    args = parser.parse_args()

    # Lazy imports so `cicdctl --help` is fast and never touches
    # helm/kubectl (i.e. the help screen works on a fresh box
    # before helm is even installed).
    # Importing `apps` triggers @register for every app
    # implementation.
    from .lib.container import Container

    # Force-import every app so its @register runs. We list
    # the apps explicitly so adding a new app requires editing
    # this file (single-source-of-truth for "what does this
    # version ship with").
    from .lib.apps import gitea as _gitea  # noqa: F401
    from .lib.apps import gitea_runner as _gitea_runner  # noqa: F401
    from .lib.apps import (
        vaultwarden_k8s_sync as _vaultwarden_k8s_sync,  # noqa: F401
    )

    container = Container.production(
        proxmox_k3s_repo=args.proxmox_k3s_repo,
        repo_root=REPO_ROOT_DEFAULT,
    )
    orch = container.orchestrator
    assert orch is not None  # production() always sets it

    container.logger.info(
        "cli.started",
        command=args.command,
        cluster=args.cluster,
        proxmox_k3s_repo=str(args.proxmox_k3s_repo),
    )

    exit_code = EXIT_PREREQ
    if args.command == "plan":
        exit_code = orch.plan(args.cluster)
    elif args.command == "apply":
        if not args.auto_approve:
            print(
                f"About to install app catalog for cluster "
                f"'{args.cluster}'."
            )
            print("Pass --auto-approve to skip this prompt.")
            sys.exit(EXIT_OK)
        exit_code = orch.apply(args.cluster)
    elif args.command == "destroy":
        if not args.auto_approve:
            print(
                f"About to DESTROY app catalog for cluster "
                f"'{args.cluster}'."
            )
            print("Pass --auto-approve to skip this prompt.")
            sys.exit(EXIT_OK)
        exit_code = orch.destroy(args.cluster)
    elif args.command == "status":
        exit_code = orch.status(args.cluster)
    elif args.command == "validate":
        exit_code = orch.validate(args.cluster)
    else:
        parser.error(f"unknown command: {args.command}")

    container.logger.info(
        "cli.finished",
        command=args.command,
        cluster=args.cluster,
        exit_code=exit_code,
    )
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())
