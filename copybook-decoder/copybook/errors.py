"""Exception hierarchy for the copybook decoder."""

from __future__ import annotations


class CopybookError(Exception):
    """Base class for all errors raised by this package."""


class CopybookSyntaxError(CopybookError):
    """A copybook layout definition is invalid or uses unsupported syntax."""


class ManifestError(CopybookError):
    """The receiving manifest is invalid, or no unambiguous layout version
    exists for a (sender, business_date) pair.  Never guess a version."""


class RecordLengthError(CopybookError):
    """A data line does not match the fixed record length of the layout."""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"record length mismatch: expected {expected} bytes, got {actual}"
        )


class FieldDecodeError(CopybookError):
    """A single field could not be decoded.

    ``offset`` is the byte offset of the offending field *within the record*
    (0-based).  The pipeline converts it to an absolute file offset.
    """

    def __init__(self, field: str, offset: int, reason: str):
        self.field = field
        self.offset = offset
        self.reason = reason
        super().__init__(f"field {field} at record byte {offset}: {reason}")
