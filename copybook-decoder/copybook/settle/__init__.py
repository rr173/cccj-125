"""Open-item settlement over decoded bill, receipt and cancellation events.

This subpackage turns the already-decoded financial event stream into a
replayable write-off (allocation) ledger.  It never talks to a payment
system and never re-decodes raw copybook bytes: every input is a JSONL
event wrapped in ``meta`` / ``fields`` (the shape produced by
:meth:`DecodeResult.write_jsonl`), potentially spread over several files.

Events are ordered by their UTC instant and, for equal instants, by the
UTF-8 byte order of ``event_id``; the physical file order and chunking can
therefore never change the result.  Each applied event first completes its
own open or cancel and then triggers one automatic sweep that pairs the
earliest still-open bill with the earliest still-available receipt.

* exact money: integer cents on Python's unbounded ints, never float;
* full history: every allocation edge keeps its open and release, and a
  re-match between the same two objects is a new edge, never an overwrite;
* cutoff semantics: only events ``at <= cutoff`` are applied, so later
  cancels cannot reach back into an earlier balance picture;
* containment: routing faults abort the whole run; business faults only
  invalidate their own (account, currency) group, whose partial replay is
  never presented as a complete balance.
"""

from .config import ROUTES, FieldPath, SettleConfig
from .engine import (ALLOCATIONS_JSONL, EVENTS_JSONL, INVALID_GROUPS_JSONL,
                     OBJECTS_JSONL, REPORT_JSON, EXIT_BUSINESS_REJECT,
                     EXIT_CANNOT_PROCESS, EXIT_OK, run_settlement)
from .errors import (SettleConfigError, SettleError, SettleInputError,
                     SettleQueryError)
from .events import (BILL, CANCEL_BILL, CANCEL_RECEIPT, CURRENCIES,
                     EVENT_KINDS, RECEIPT, RawEvent, load_events)
from .ledger import (ACTIVE, CANCELLED, OPEN, RELEASED,
                     REASON_BILL_CANCELLED, REASON_RECEIPT_CANCELLED,
                     GroupProcessor)
from .trace import trace_object
from .values import (AGING_BUCKETS, age_bucket, age_in_days, format_cents,
                     parse_cents, parse_due_date, parse_timestamp)

__all__ = [
    "ACTIVE", "AGING_BUCKETS", "ALLOCATIONS_JSONL", "BILL", "CANCELLED",
    "CANCEL_BILL", "CANCEL_RECEIPT", "CURRENCIES", "EVENT_KINDS",
    "EVENTS_JSONL", "EXIT_BUSINESS_REJECT", "EXIT_CANNOT_PROCESS",
    "EXIT_OK", "FieldPath", "GroupProcessor", "INVALID_GROUPS_JSONL",
    "OBJECTS_JSONL", "OPEN", "REASON_BILL_CANCELLED",
    "REASON_RECEIPT_CANCELLED", "RECEIPT", "RELEASED", "REPORT_JSON",
    "ROUTES", "RawEvent", "SettleConfig", "SettleConfigError",
    "SettleError", "SettleInputError", "SettleQueryError", "age_bucket",
    "age_in_days", "format_cents", "load_events", "parse_cents",
    "parse_due_date", "parse_timestamp", "run_settlement", "trace_object",
]
