"""Exception hierarchy for the batch handoff auditor."""

from __future__ import annotations

from ..errors import CopybookError


class AuditError(CopybookError):
    """Base class for auditor failures."""


class AuditConfigError(AuditError):
    """The audit configuration (paths / categories) is unusable."""


class AuditInputError(AuditError):
    """The two evidence files do not form one complete, ordered proof set.

    Duplicated or missing line numbers, out-of-range lines, source
    mismatches, inverted byte ranges, malformed JSON or a ``raw_hex`` entry
    masquerading as a normal record all make the whole run *unverifiable*;
    no acceptance result is produced.
    """
