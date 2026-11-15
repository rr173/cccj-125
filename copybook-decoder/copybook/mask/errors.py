"""Exception hierarchy for the masking sample generator."""

from __future__ import annotations

from ..errors import CopybookError


class MaskError(CopybookError):
    """Base class for masking failures.

    Any MaskError aborts the whole run before (or without) leaving a
    delivery behind: a masking run either completes fully or leaves no
    output directory at all.
    """


class MaskPolicyError(MaskError):
    """The field policy is unusable.

    Unknown actions, malformed paths, duplicated paths, a path that never
    matches any input record, a path that resolves to a container, or a
    scalar value whose type does not fit the declared action are all
    policy failures.  They are never silently skipped.
    """


class MaskInputError(MaskError):
    """An input JSONL file is corrupt or structurally unusable."""


class MaskKeyError(MaskError):
    """The mapping key is unavailable, too weak, or does not match the
    fingerprint declared by the policy."""


class MaskConflictError(MaskError):
    """Two distinct original values in one link group would collapse onto
    the same pseudonym.  The run refuses to merge identities."""
