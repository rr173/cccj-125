"""Batch handoff auditing across multi-line control records.

This subpackage validates the FHEAD / BHEAD / DETAIL / BTAIL / FTAIL
handoff protocol over evidence already produced by the decoder: the normal
record JSONL (``write_jsonl``) and the quarantine index
(``export_quarantine``).  It never decodes raw fields itself and never
extends the copybook grammar -- it only walks decoded ``fields`` via
string-array paths, uses SQLite solely for disk staging and batch-id
deduplication, and streams details so that no whole batch or whole file
has to fit in memory.
"""

from .config import (MILLI_SCALE, AuditConfig, FieldPath, format_milli,
                     parse_count, parse_quantity_milli)
from .engine import (ACCEPTED, BUSINESS_REJECTED, PASS, REJECTED,
                     UNVERIFIABLE, AuditEngine, run_audit)
from .errors import AuditConfigError, AuditError, AuditInputError
from .evidence import Evidence, iter_evidence

__all__ = [
    "ACCEPTED", "AuditConfig", "AuditConfigError", "AuditEngine",
    "AuditError", "AuditInputError", "BUSINESS_REJECTED", "Evidence",
    "FieldPath", "MILLI_SCALE", "PASS", "REJECTED", "UNVERIFIABLE",
    "format_milli", "iter_evidence", "parse_count", "parse_quantity_milli",
    "run_audit",
]
