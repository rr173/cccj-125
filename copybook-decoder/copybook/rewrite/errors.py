"""Exception hierarchy for the fixed-point original-file rewrite feature.

Every failure aborts the whole job before a delivery directory is
created (or removes a half-finished one).  There is no partial success:
a caller either gets a complete rewritten original plus matching
evidence, or nothing that can be mistaken for a finished delivery.
"""

from __future__ import annotations

from ..errors import CopybookError


class RewriteError(CopybookError):
    """Base class for rewrite failures."""


class RewriteOrderError(RewriteError):
    """The correction order JSON is malformed or semantically unusable.

    Missing required members, duplicate JSON object members, NaN/Infinity
    or float values, wrong member types, unknown layouts, paths that do
    not resolve to a single elementary non-FILLER field, and illegal
    coordinates all land here.
    """


class RewriteValueError(RewriteError):
    """A new value cannot be canonically encoded into the field capacity.

    The string is not a legal decimal for a numeric PIC, carries too many
    fraction digits, overflows the digit capacity, is negative for an
    unsigned PIC, or is not representable in the field's single-byte
    codec for a text PIC.
    """


class RewriteConflictError(RewriteError):
    """Two edits demand different new bytes for a shared position.

    Overlapping field spans are legal only while the overlapping
    positions agree.  This error means the overlap disagrees, so the
    whole job is refused rather than letting one edit win.
    """


class RewriteChecksumError(RewriteError):
    """A declared SHA-256 (whole file or per-record) does not match the
    digest computed from the actual original bytes."""


class RewriteIOError(RewriteError):
    """A streaming read or write, the output directory check, or the
    atomic delivery move failed."""
