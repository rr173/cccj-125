"""Exception hierarchy for the open-item settlement processor."""

from __future__ import annotations

from ..errors import CopybookError


class SettleError(CopybookError):
    """Base class for settlement failures."""


class SettleConfigError(SettleError):
    """The field-path configuration is unusable."""


class SettleInputError(SettleError):
    """The whole input set cannot be processed.

    Malformed JSON, records without the meta/fields wrapper, missing or
    invalid routing fields (event_id/account/currency/at/kind), duplicate
    event ids, an unparseable cutoff or an output directory that already
    exists all abort the run before any formal result is delivered.
    """


class SettleQueryError(SettleError):
    """An object-tracing query against a delivered result cannot be met."""
