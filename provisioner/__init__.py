"""provisioner — orchestrator for the proxmox-cicd app catalog.

Mirrors proxmox-vms/provisioner/ and proxmox-k3s/provisioner/ in
shape and intent. The orchestrator wires a `Container` of
HelmRunner + KubectlRunner + StructuredLogger behind Protocols
and iterates over the app registry to install or remove each
enabled app.
"""

__version__ = "0.1.0"
