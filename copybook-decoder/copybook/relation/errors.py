"""Exception hierarchy for the relational admission processor."""

from __future__ import annotations

from ..errors import CopybookError


class RelationError(CopybookError):
    """Base class for relational admission failures."""


class RelationConfigError(RelationError):
    """The reference contract is unusable.

    Contract faults are rejected before any input file is read and before
    anything is executed: no SQL or Python expression is ever built from
    contract strings, and no partial delivery is produced.
    """


class RelationInputError(RelationError):
    """The whole handoff cannot be processed (structural input fault).

    Bad JSON, duplicate JSON object members, an invalid ``meta`` /
    ``fields`` wrapper, missing or non-unique row ids, invalid meta
    offsets and an output directory that already exists all abort the run.
    These are not ordinary bad business rows: no formal result directory is
    delivered and the completion marker never appears.
    """


class RelationQueryError(RelationError):
    """An explanation/table query against a delivery cannot be met."""
