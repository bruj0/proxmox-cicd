"""groups/base — BaseGroup ABC + CyclicGroupError.

A `BaseGroup` is a named DAG of apps. The orchestrator
(WP3) topologically sorts the DAG at apply time and
applies each app in turn; cycles raise `CyclicGroupError`
and exit with `EXIT_CATALOG`.

This module is the contract every concrete group
subclass must satisfy. The contract is deliberately
small:

    name: str                    # unique registry key
    nodes: dict[str, str]        # app_name -> human note
    edges: dict[str, list[str]]  # app_name -> deps
    enabled_in(catalog) -> bool  # gate the orchestrator checks

The empty-`nodes` sentinel means "use every enabled
app in catalog order" (today's behaviour). This is the
`DefaultGroup` contract in §5.5; concrete groups always
declare nodes explicitly.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from ..catalog import CatalogError

if TYPE_CHECKING:
    from ..catalog import Catalog


class CyclicGroupError(CatalogError):
    """Raised when a group's DAG contains a cycle.

    Subclassing `CatalogError` (rather than a fresh
    exception) means the orchestrator's existing
    "catalog failure -> EXIT_CATALOG" path catches it
    without a new branch.
    """


class BaseGroup(abc.ABC):
    """Base class for a named DAG of apps.

    The orchestrator instantiates a concrete subclass
    exactly once per `--group` invocation (no
    per-cluster state on the group itself). `nodes`
    and `edges` are properties so subclasses can
    compute them dynamically if they want, even though
    the shipped groups use literal dicts.
    """

    #: Unique registry key. Concrete subclasses must
    #: override. The registry refuses two groups with
    #: the same name from different modules.
    name: str = ""

    #: Short human description for `cicdctl plan --group X`
    description: str = ""

    @property
    @abc.abstractmethod
    def nodes(self) -> dict[str, str]:
        """App-name -> human note.

        For `cicdctl plan` output: the note is printed
        next to each `+ app:` line so the operator can
        read the rationale at a glance.
        """

    @property
    @abc.abstractmethod
    def edges(self) -> dict[str, list[str]]:
        """App-name -> list of dependencies.

        `edges[A] = [B, C]` means "A depends on B and C"
        (B and C must apply before A). Apps not in
        `edges` are roots (no deps); apps with an
        empty list are also roots.
        """

    @abc.abstractmethod
    def enabled_in(self, catalog: Catalog) -> bool:
        """Return True if this group can run for `catalog`.

        Concrete groups use this to gate themselves on
        prerequisites (e.g. `cicd-stack` requires
        `gitea` to be enabled). The orchestrator prints
        a clear "group requires X" message when the
        gate is closed.
        """

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a `BaseGroup` subclass that hasn't
        declared a `name`. Mirrors the
        `BaseApp.__init_subclass__` rule from WP0.
        """
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "name", ""):
            raise TypeError(
                f"{cls.__name__} must define a non-empty "
                f"`name` class attr to be a BaseGroup."
            )
