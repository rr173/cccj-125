"""Offline Git handoff intake: a self-contained bundle becomes queryable
commit, directory and file-version facts, and one blob at a time can be
extracted for the existing decoder.

The reader never shells out to ``git``/libgit2/Dulwich, never checks out a
working tree, never runs hooks, and never opens a path from inside the
bundle as a host path.  Only Python's standard library is used to parse
the bundle, the pack streams, the delta instructions and the object
payloads.
"""

from .bundle import (BundleLayout, BundleRef, PackEntry, PackLayout,
                     extract_pack, parse_pack, read_bundle_layout)
from .diff import (KIND_ADDED, KIND_DELETED, KIND_MODIFIED, KIND_UNCHANGED,
                   diff_against, diff_all_parents, file_map, file_versions)
from .errors import (BundleFormatError, CommitGraphError, DeltaError,
                     ExportError, GitHandoffError, HandoffQueryError,
                     ObjectFormatError, ObjectStoreError, PackFormatError,
                     ResourceLimitError, TreeFormatError)
from .graph import (CommitGraph, CommitNode, GitView, decode_path,
                    encode_path, path_display)
from .handoff import (COMMITS_DIR, COMMITS_JSONL, EXIT_OK, EXIT_UNUSABLE,
                      EXPORT_MANIFEST_JSON, REPORT_JSON, export_blob,
                      ingest_handoff, inspect_handoff)
from .objects import (MODE_DIRECTORY, MODE_EXECUTABLE, MODE_GITLINK,
                      MODE_REGULAR, MODE_SYMLINK, CommitHeaders, TreeEntry,
                      parse_commit, parse_tree)
from .pack import Limits, ObjectInfo, PackStore, apply_delta, object_id_for

__all__ = [
    "BundleFormatError", "BundleLayout", "BundleRef", "COMMITS_DIR",
    "COMMITS_JSONL", "CommitGraph", "CommitGraphError", "CommitHeaders",
    "CommitNode", "DeltaError", "EXIT_OK", "EXIT_UNUSABLE",
    "EXPORT_MANIFEST_JSON", "ExportError", "GitHandoffError", "GitView",
    "HandoffQueryError", "KIND_ADDED", "KIND_DELETED", "KIND_MODIFIED",
    "KIND_UNCHANGED", "Limits", "MODE_DIRECTORY", "MODE_EXECUTABLE",
    "MODE_GITLINK", "MODE_REGULAR", "MODE_SYMLINK", "ObjectFormatError",
    "ObjectInfo", "ObjectStoreError", "PackEntry", "PackFormatError",
    "PackLayout", "PackStore", "REPORT_JSON", "ResourceLimitError",
    "TreeEntry", "TreeFormatError", "apply_delta", "decode_path",
    "diff_against", "diff_all_parents", "encode_path", "export_blob",
    "extract_pack", "file_map", "file_versions", "ingest_handoff",
    "inspect_handoff", "object_id_for", "parse_commit", "parse_pack",
    "parse_tree", "path_display", "read_bundle_layout",
]
