"""helm_post_renderers — post-renderer scripts invoked via
`helm upgrade --install --post-renderer <script>`.

Why these exist
===============

The orchestrator installs several upstream helm charts that
each create their own `Secret` resources. Some of those
Secrets are also the source-of-truth for
VaultwardenK8sSync (VKS), which writes its own labels and
annotations when it manages the Secret (`managed-by:
vaultwarden-kubernetes-secrets`, etc.).

When both controllers want to write labels on the same
Secret, kubectl server-side apply raises a field-manager
conflict:

    conflict with "unknown" using v1:
      .metadata.labels.app.kubernetes.io/managed-by

The orchestrator's previous workaround (`--take-ownership`)
only adds `meta.helm.sh/release-name` annotations; it
doesn't overwrite VKS's labels, so the next apply still
fails once VKS has synced at least once.

The fix is to have helm **not own** the chart-managed
Secret at all. We do this via a post-renderer that strips
the helm-emitted labels from the Secret manifest before
kubectl applies it. The chart's Secret body is still
written — kubectl creates or updates it without a
field-manager conflict because no chart-side labels are
being asserted. VKS subsequently writes its own labels
and helm never tries to assert them back.

The post-renderer is invoked by helm as a child process:
it reads the rendered manifest stream on stdin and writes
the modified manifest stream to stdout. Helm pipes its
output through the post-renderer and consumes stdout as
the final manifest.

Each post-renderer is a standalone script (chmod +x,
python3 shebang) so helm can fork it directly. They are
also importable as Python modules so the unit tests can
exercise them without subprocess plumbing.
"""

from __future__ import annotations
