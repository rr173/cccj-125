"""De-identified sample generation over decoded JSONL records.

Downstream teams need realistic fixed-width account flows to verify
reports and integration programs, but customer names, account numbers and
balances must not be copied into test environments.  This subpackage
reads *already decoded* JSONL records (the shape produced by
:meth:`DecodeResult.write_jsonl`) plus a field policy, and delivers a
masked JSONL dataset together with a report explaining how every record
was handled.  It never re-reads the original EBCDIC files, never compares
field layouts and never judges whether a batch may enter the warehouse.

* Account-like keys are mapped to stable, format-preserving fakes: the
  same original yields the same fake across every input file sharing a
  policy ``group``; distinct originals are never merged (a collision
  aborts the run).
* Free text such as names is replaced by synthetic values containing no
  fragment of the original.
* Amounts and dates can be deterministically shifted (key-derived offset)
  or generalised into ranges; null values and missing fields are handled
  separately and counted separately.
* Fields not listed in the policy pass through unchanged.  Policy paths
  that match nothing, container targets, type mismatches and
  uniqueness-breaking rules are errors, never silent skips.

The mapping key comes from the caller (environment variable or file) and
is never written to outputs, diagnostics, logs or reports; no original
sensitive values or reversible mapping tables are persisted.
"""

from .engine import (DEFAULT_CHUNK_SIZE, NOTES, RECORD_REPORT,
                     REPORT_JSON, REPORT_TXT, run_mask)
from .errors import (MaskConflictError, MaskError, MaskInputError,
                     MaskKeyError, MaskPolicyError)
from .mapping import MaskKey, key_fingerprint
from .policy import (BUCKETIZE, PSEUDONYM, SHIFT, SYNTHESIZE, FieldRule,
                     MaskPolicy)

__all__ = [
    "BUCKETIZE", "DEFAULT_CHUNK_SIZE", "FieldRule", "MaskConflictError",
    "MaskError", "MaskInputError", "MaskKey", "MaskKeyError",
    "MaskPolicy", "MaskPolicyError", "NOTES", "PSEUDONYM", "RECORD_REPORT",
    "REPORT_JSON", "REPORT_TXT", "SHIFT", "SYNTHESIZE", "key_fingerprint",
    "run_mask",
]
