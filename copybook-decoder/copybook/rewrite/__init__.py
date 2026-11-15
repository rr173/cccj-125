"""Fixed-point write-back of confirmed corrections onto an original.

The rewrite feature takes the original binary plus a UTF-8 JSON
correction order and produces a *new* original that differs only in the
bytes the confirmed corrections occupy -- padding, unselected
REDEFINES views and non-standard but legal sign spellings outside the
targeted spans are preserved byte for byte.  Nothing is re-encoded as a
whole: each edit is validated against the field's original bytes
(``expected_hex``) and turned into a canonical, capacity-checked byte
requirement.

See :mod:`copybook.rewrite.engine` for the streaming pipeline and the
evidence contract.
"""

from .encoder import encode_value
from .engine import (DEFAULT_CHUNK_SIZE, DEFAULT_MAX_RECORD_BYTES,
                     EVIDENCE_NAME, FORMAT, OUTPUT_NAME,
                     PreparedEdit, PreparedRecord, rewrite_original)
from .errors import (RewriteChecksumError, RewriteConflictError,
                     RewriteError, RewriteIOError, RewriteOrderError,
                     RewriteValueError)
from .order import (CorrectionOrder, EditSpec, LayoutEntry, RecordSpec,
                    SUPPORTED_CODECS, load_order)
from .pathres import ResolvedField, resolve_path

__all__ = [
    "DEFAULT_CHUNK_SIZE", "DEFAULT_MAX_RECORD_BYTES", "CorrectionOrder",
    "EVIDENCE_NAME", "EditSpec", "FORMAT", "LayoutEntry",
    "PreparedEdit", "PreparedRecord", "RecordSpec", "ResolvedField",
    "RewriteChecksumError", "RewriteConflictError", "RewriteError",
    "RewriteIOError", "RewriteOrderError", "RewriteValueError",
    "SUPPORTED_CODECS", "encode_value", "load_order",
    "resolve_path", "rewrite_original",
]
