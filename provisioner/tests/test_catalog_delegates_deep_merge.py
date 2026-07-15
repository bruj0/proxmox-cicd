"""test_catalog_delegates_deep_merge — WP9 guard.

After WP9 the canonical deep-merge helper lives on
`BaseApp._deep_merge` (see `apps/base.py`). The
catalog loader still exposes a private `_deep_merge`
for backward compatibility (the WP1-era call sites in
`catalog.py` use it), but the implementation
*delegates* to `BaseApp._deep_merge` so the merge
behaviour lives in exactly one place.

If a future contributor re-implements the merge
logic in `catalog.py` (drift), this test catches it
because the assertions compare the outputs of the
two helpers across a representative set of inputs.
"""

from __future__ import annotations

from typing import Any

import pytest

from provisioner.lib.apps.base import BaseApp
from provisioner.lib.catalog import _deep_merge as catalog_deep_merge


@pytest.mark.parametrize(
    "left, right",
    [
        # Simple override
        ({"a": 1, "b": 2}, {"b": 99}),
        # Nested override
        ({"path": {"foo": 1, "bar": 2}, "other": "keep"}, {"path": {"foo": 99}}),
        # Right-wins for non-dict vs dict
        ({"a": "scalar"}, {"a": {"nested": True}}),
        # Empty overlay
        ({"a": 1, "b": 2}, {}),
        # Empty base
        ({}, {"a": 1, "b": 2}),
        # Both empty
        ({}, {}),
    ],
    ids=[
        "simple-override",
        "nested-override",
        "scalar-vs-dict",
        "empty-overlay",
        "empty-base",
        "both-empty",
    ],
)
def test_catalog_deep_merge_matches_base_app(
    left: dict[str, Any], right: dict[str, Any]
) -> None:
    """`catalog._deep_merge` must produce identical
    output to `BaseApp._deep_merge` across a
    representative input set. Drift here is a
    regression in the WP9 single-source-of-truth
    contract."""
    catalog_out = catalog_deep_merge(left, right)
    baseapp_out = BaseApp._deep_merge(left, right)
    assert catalog_out == baseapp_out
