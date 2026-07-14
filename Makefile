# Makefile — proxmox-cicd pipeline (stage 3, SOLID app catalog).
#
# Targets mirror proxmox-vms/Makefile and proxmox-k3s/Makefile so
# operators who know one repo know all three:
#
#   make plan      — diff desired app set against live cluster (no changes)
#   make apply     — install the apps (idempotent; plan then apply)
#   make destroy   — uninstall every app + delete its namespace
#   make status    — read apps.json + query each app's status
#   make validate  — parse catalog.yaml + values/*.yaml, no kubectl/helm
#   make test      — run pytest
#   make lint      — run ruff + mypy
#   make clean     — remove logs/ + .mypy_cache/ + .ruff_cache/

SHELL := /bin/bash
PYTHON ?= python

# .env is gitignored; -include so the makefile works without one.
-include .env
export

# Default CLUSTER. Override on the command line: `make apply CLUSTER=cicd`.
CLUSTER ?= cicd

# Path to the sibling proxmox-k3s repo. We read its
# infra/clusters/<name>/kubeconfig.yaml to talk to the apiserver.
PROXMOX_K3S_REPO ?= $(PWD)/../proxmox-k3s

# Path to the SSH private key for the PVE jump box (used only if
# PROXMOX_K3S_REPO doesn't already have a usable kubeconfig.yaml).
SSH_KEY ?= ~/.ssh/id_ed25519

# ------------------------------------------------------ public targets

.PHONY: plan apply destroy status validate test lint clean help

help:
	@echo "Targets:"
	@echo "  plan     [CLUSTER=<name>]  -- diff desired vs live apps (no changes)"
	@echo "  apply    [CLUSTER=<name>]  -- install app catalog (idempotent)"
	@echo "  destroy  [CLUSTER=<name>]  -- uninstall apps + delete namespaces"
	@echo "  status   [CLUSTER=<name>]  -- show live app state"
	@echo "  validate [CLUSTER=<name>]  -- parse catalog + values, no kubectl/helm"
	@echo "  test            -- run pytest"
	@echo "  lint            -- run ruff + mypy"
	@echo "  clean           -- remove logs/ + .pytest_cache/ + .mypy_cache/ + .ruff_cache/"

plan:
	@$(PYTHON) -m provisioner \
		--proxmox-k3s-repo $(PROXMOX_K3S_REPO) \
		plan $(CLUSTER)

apply:
	@$(PYTHON) -m provisioner \
		--proxmox-k3s-repo $(PROXMOX_K3S_REPO) \
		apply $(CLUSTER) \
		--auto-approve

destroy:
	@$(PYTHON) -m provisioner \
		--proxmox-k3s-repo $(PROXMOX_K3S_REPO) \
		destroy $(CLUSTER) \
		--auto-approve

status:
	@$(PYTHON) -m provisioner \
		--proxmox-k3s-repo $(PROXMOX_K3S_REPO) \
		status $(CLUSTER)

validate:
	@$(PYTHON) -m provisioner validate $(CLUSTER)

# ------------------------------------------------------ internal targets

test:
	@uv run --quiet pytest provisioner/tests/ scripts/tests/ -q

lint:
	@$(PYTHON) -m ruff check provisioner/   @$(PYTHON) -m mypy provisioner/lib/

install-deps:
	@$(PYTHON) -m pip install --user pytest ruff mypy

clean:
	@rm -rf logs/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"