"""Relational admission processing for a multi-table handoff.

This subpackage answers one offline data-quality question: given several
already-decoded logical tables (JSONL files in the standard ``meta`` /
``fields`` wrapper, possibly several files per table) and a reference
contract (declared columns, named candidate keys, named references), which
records still have *enough support* to enter the warehouse, and for every
record that does not, what is the independently checkable proof that it
lost its support?

The delivered set is the greatest fixed point of "a row is kept when every
mandatory (and every non-all-null optional) reference resolves to at least
one kept parent whose business key tuple matches component by component":

* closed reference cycles are kept as a whole -- rows are never picked as
  traversal anchors and never deleted merely to break a cycle;
* rows with local field faults (layer 0) never support anyone, but their
  still-readable key tuples do count as initial parent candidates;
* every later rejection receives the earliest layer at which it is
  provable and one stable primary proof reference (UTF-8 byte order of the
  reference name on ties);
* proof nodes are keyed by the unsupported ``(table, reference, key
  tuple)`` group, so one shared root cause -- including diamond-shaped
  fan-in and a huge same-key candidate group -- is stored once and only
  referenced by its dependents; proof text never expands exponentially.

Everything that scales (records, business keys, references, proof nodes
and edges) is staged in an on-disk SQLite database; the Python layer never
holds a whole table, the complete key space, one huge same-key candidate
group or the full dependency graph.  The delivery directory is
self-describing: a reopened process explains any row using only the files
inside it, never the original inputs.
"""

from .config import (CANDIDATE, INTEGER, MANDATORY, OPTIONAL, STRING,
                     FieldDecl, FieldPath, Reference, RelationContract,
                     TableContract)
from .engine import (COMPLETE_MARKER, EXIT_BUSINESS_REJECT,
                     EXIT_CANNOT_PROCESS, EXIT_OK, LOCAL_REJECT,
                     CASCADE_REJECT, KEPT, run_admission)
from .errors import (RelationConfigError, RelationError,
                     RelationInputError, RelationQueryError)
from .query import get_proof_node, iter_kept_rows, explain_row

__all__ = [
    "CANDIDATE", "CASCADE_REJECT", "COMPLETE_MARKER", "FieldDecl",
    "FieldPath", "INTEGER", "KEPT", "LOCAL_REJECT", "MANDATORY",
    "OPTIONAL", "Reference", "RelationConfigError", "RelationContract",
    "RelationError", "RelationInputError", "RelationQueryError",
    "STRING", "TableContract", "EXIT_BUSINESS_REJECT",
    "EXIT_CANNOT_PROCESS", "EXIT_OK", "explain_row", "get_proof_node",
    "iter_kept_rows", "run_admission",
]
