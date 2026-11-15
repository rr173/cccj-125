"""Exception hierarchy for the offline Git handoff reader.

Every failure that makes a bundle, an object, a commit graph or a requested
export untrustworthy raises a subclass of :class:`GitHandoffError`.  A
partially decoded pack is **never** returned as a success: the reader
validates the whole container (header, pack framing, every stream, per
object identity, the trailing checksum and the absence of trailing bytes)
before it exposes any object at all.
"""

from __future__ import annotations

from ..errors import CopybookError


class GitHandoffError(CopybookError):
    """Base class for offline Git handoff failures."""


class BundleFormatError(GitHandoffError):
    """The bundle container itself is not a supported self-contained v2
    bundle (bad signature, non-commit refs, prerequisites, unsupported
    object format/capability, truncated or trailing bytes, bad checksum)."""


class PackFormatError(GitHandoffError):
    """The embedded version-2 pack violates its framing.

    Bad signature/version/object count, a zlib stream that does not end
    exactly at its boundary, a declared size that does not match the
    inflated size, an unknown object type or an unsupported delta form.
    """


class ObjectFormatError(GitHandoffError):
    """A stored object's content does not match its declared Git type.

    Commit and tree payloads are parsed strictly; a payload that does not
    conform is rejected instead of being guessed at.
    """


class DeltaError(GitHandoffError):
    """A delta instruction stream is inconsistent.

    Source/target size mismatch, a copy or insert that runs past the
    source/target boundary, a missing base object, a delta cycle or a
    chain deeper than the configured limit.  The base object is not
    consumed or modified: several targets may share it.
    """


class ResourceLimitError(GitHandoffError):
    """A configured resource limit was reached.

    Single-object inflation, cumulative inflation, object count and delta
    depth are hard refusals.  Nothing is ever truncated into an
    apparently valid object.
    """


class ObjectStoreError(GitHandoffError):
    """An object could not be stored, fetched or re-verified."""


class CommitGraphError(GitHandoffError):
    """The reachable commit graph is unusable.

    Missing or wrongly typed commit/tree/ref objects, a parent cycle, or a
    duplicate parent entry is reported here.
    """


class TreeFormatError(GitHandoffError):
    """A tree entry is malformed.

    Duplicate names, an empty name, a name containing ``/`` or the exact
    components ``.``/``..``, an invalid mode, an unknown mode/type pairing,
    or a tree entry that does not point at an existing object of the
    declared type.
    """


class HandoffQueryError(GitHandoffError):
    """A lookup (commit id, ref, path, manifest section) cannot be met."""


class ExportError(GitHandoffError):
    """A blob export was refused or failed.

    Unknown commit, path that is not a regular/executable blob, source
    bundle damaged, destination already present, or a write failure.  The
    destination is never left half written and existing bytes are never
    modified.
    """
