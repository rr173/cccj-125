"""Per-(account, currency) open-item ledger replay.

One :class:`GroupProcessor` owns exactly one account/currency pair and
replays the group's events in the canonical order (UTC instant, then
event_id UTF-8 bytes).  Every applied event first completes its own open
or cancel, then triggers one automatic allocation sweep inside the group.

Allocation discipline
---------------------

* the earliest bill (by opening event order) that still has an open
  balance is paired with the earliest receipt that still has available
  money, taking the smaller remaining side, until one side is exhausted;
* a receipt may cover many bills and a bill may be covered by many
  receipts; every actual transfer is stored as its own allocation edge;
* cancelling a receipt releases only the allocations that receipt still
  supports, returns the money to the affected bills and drops the
  receipt's unallocated remainder;
* cancelling a bill releases *all* of its active allocations back to the
  original receipts (which keep their original opening order), removes
  the bill, then re-runs the sweep so the released money is redistributed
  to the remaining open items;
* released edges are never deleted or reused: a later re-match between
  the same two objects gets a brand new allocation id.

All arithmetic is integer cents on Python's unbounded ints, so amounts
above 64 bits are exact.  The first business fault invalidates the whole
group; the partial replay is discarded and the group never reports
balances, aging or active allocations.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Optional

from .config import SettleConfig
from .events import (BILL, CANCEL_BILL, CANCEL_RECEIPT, RECEIPT,
                     CANCEL_TARGET_KIND, RawEvent)
from .values import (AGING_BUCKETS, age_bucket, age_in_days, format_cents,
                     parse_cents, parse_due_date, require_nonempty_text)

#: Object / allocation life-cycle statuses.
OPEN = "open"
CANCELLED = "cancelled"
ACTIVE = "active"
RELEASED = "released"

#: Release reasons recorded on a released allocation edge.
REASON_RECEIPT_CANCELLED = "receipt_cancelled"
REASON_BILL_CANCELLED = "bill_cancelled"


class _GroupInvalid(Exception):
    """Internal control flow: the current group is invalid from here on."""

    def __init__(self, error: dict):
        super().__init__(error["message"])
        self.error = error


@dataclass
class _Object:
    kind: str                         # BILL | RECEIPT
    object_id: str
    opening_event_id: str
    opening_at_utc: object
    opening_seq: int
    original_cents: int
    remaining_cents: int              # bill: open balance; receipt: available
    status: str = OPEN
    cancelled_event_id: Optional[str] = None
    cancelled_seq: Optional[int] = None
    due_on: Optional[object] = None   # bills only


@dataclass
class _Allocation:
    allocation_id: str
    bill_id: str
    receipt_id: str
    amount_cents: int
    opened_event_id: str
    opened_seq: int
    status: str = ACTIVE
    released_event_id: Optional[str] = None
    released_seq: Optional[int] = None
    release_reason: Optional[str] = None


@dataclass
class GroupError:
    """One localized business fault that invalidated a group."""

    code: str
    message: str
    event_id: str
    source_file: str
    line_number: int
    field_route: Optional[str] = None
    detail: dict = dc_field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "event_id": self.event_id,
            "source_file": self.source_file,
            "line_number": self.line_number,
            "field_route": self.field_route,
            "detail": self.detail,
        }


class GroupProcessor:
    """Replays one account/currency group and records every edge."""

    def __init__(self, account: str, currency: str,
                 config: SettleConfig):
        self.account = account
        self.currency = currency
        self.config = config
        # Opening order is the dict insertion order, which mirrors the
        # canonical event order; cancelled objects stay in the dicts so
        # repeated cancels and back-references can be diagnosed precisely.
        self.bills: dict[str, _Object] = {}
        self.receipts: dict[str, _Object] = {}
        self.allocations: list[_Allocation] = []
        self._allocation_counter = 0
        self.errors: list[GroupError] = []
        self.invalid = False
        # event_id -> the effect dict handed back to the runner.
        self.effects: dict[str, dict] = {}

    # ---------------------------------------------------------- diagnostics
    def _fail(self, event: RawEvent, code: str, message: str, *,
              field_route: Optional[str] = None,
              detail: Optional[dict] = None) -> None:
        error = GroupError(
            code=code, message=message, event_id=event.event_id,
            source_file=event.source_file, line_number=event.line_number,
            field_route=field_route, detail=detail or {})
        self.errors.append(error)
        self.invalid = True
        raise _GroupInvalid(error.as_dict())

    def _text(self, route: str, event: RawEvent):
        return self.config.get(route).get(event.fields)

    # ----------------------------------------------------------------- run
    def run(self, events: list[RawEvent], cutoff_utc) -> None:
        """Apply every event at or before *cutoff_utc* in canonical order."""
        seq = 0
        for event in events:
            if event.at_utc > cutoff_utc:
                # Later events, including later cancels, must not touch the
                # cutoff picture; they are not business-validated either.
                continue
            seq += 1
            try:
                if event.kind == BILL:
                    self._open_bill(event, seq)
                elif event.kind == RECEIPT:
                    self._open_receipt(event, seq)
                elif event.kind == CANCEL_RECEIPT:
                    self._cancel_receipt(event, seq)
                else:
                    self._cancel_bill(event, seq)
            except _GroupInvalid:
                # The group is invalid. Discard the partial replay: no
                # balances, aging or active allocations are delivered.
                return
            # Each successfully applied event ends with one sweep.
            self._auto_match(event, seq)

    # ------------------------------------------------------------- opening
    def _check_reused_id(self, event: RawEvent, object_id: str,
                         registry: dict[str, _Object], kind: str) -> None:
        existing = registry.get(object_id)
        if existing is not None:
            self._fail(
                event, "duplicate_object_id",
                f"{kind} object_id {object_id!r} already opened by event "
                f"{existing.opening_event_id!r}; ids may not be reused "
                "within an account/currency, even after cancellation",
                field_route="object_id",
                detail={"object_id": object_id, "kind": kind,
                        "previous_event_id": existing.opening_event_id,
                        "previous_status": existing.status})

    def _open_bill(self, event: RawEvent, seq: int) -> None:
        object_id = self._required_id(event, "object_id")
        self._check_reused_id(event, object_id, self.bills, BILL)
        cents = self._required_amount(event)
        due_on = self._required_due_on(event)
        self.bills[object_id] = _Object(
            kind=BILL, object_id=object_id,
            opening_event_id=event.event_id, opening_at_utc=event.at_utc,
            opening_seq=seq, original_cents=cents, remaining_cents=cents,
            due_on=due_on)
        self.effects[event.event_id] = {
            "seq": seq, "action": "open_bill", "object_id": object_id,
            "opened_allocation_ids": [], "released_allocation_ids": []}

    def _open_receipt(self, event: RawEvent, seq: int) -> None:
        object_id = self._required_id(event, "object_id")
        self._check_reused_id(event, object_id, self.receipts, RECEIPT)
        cents = self._required_amount(event)
        self.receipts[object_id] = _Object(
            kind=RECEIPT, object_id=object_id,
            opening_event_id=event.event_id, opening_at_utc=event.at_utc,
            opening_seq=seq, original_cents=cents, remaining_cents=cents)
        self.effects[event.event_id] = {
            "seq": seq, "action": "open_receipt", "object_id": object_id,
            "opened_allocation_ids": [], "released_allocation_ids": []}

    def _required_id(self, event: RawEvent, route: str) -> str:
        value = self._text(route, event)
        try:
            return require_nonempty_text(value, route)
        except ValueError as exc:
            self._fail(event, "invalid_object_id", str(exc),
                       field_route=route, detail={"actual": value})

    def _required_amount(self, event: RawEvent) -> int:
        value = self._text("amount", event)
        try:
            return parse_cents(value)
        except ValueError as exc:
            self._fail(event, "invalid_amount", str(exc),
                       field_route="amount", detail={"actual": value})

    def _required_due_on(self, event: RawEvent):
        value = self._text("due_on", event)
        try:
            return parse_due_date(value)
        except ValueError as exc:
            self._fail(event, "invalid_due_on", str(exc),
                       field_route="due_on", detail={"actual": value})

    # ------------------------------------------------------------ cancels
    def _resolve_cancel_target(self, event: RawEvent,
                               target_kind: str,
                               registry: dict[str, _Object],
                               seq: int) -> _Object:
        value = self._text("target_id", event)
        try:
            target_id = require_nonempty_text(value, "target_id")
        except ValueError as exc:
            self._fail(event, "invalid_target_id", str(exc),
                       field_route="target_id", detail={"actual": value})
        target = registry.get(target_id)
        if target is None:
            self._fail(
                event, "unknown_target",
                f"{event.kind} references {target_kind} {target_id!r}, which "
                "has no open of the same account/currency at this point",
                field_route="target_id",
                detail={"target_id": target_id, "expected_kind": target_kind})
        if target.status == CANCELLED:
            self._fail(
                event, "already_cancelled",
                f"{target_kind} {target_id!r} was already cancelled by event "
                f"{target.cancelled_event_id!r}; double cancellation is not "
                "allowed",
                field_route="target_id",
                detail={"target_id": target_id,
                        "cancelled_by": target.cancelled_event_id})
        # The registry only contains objects opened by earlier applied
        # events, but keep an explicit sequence guard so a future ordering
        # bug could never create a forward reference silently.
        if target.opening_seq >= seq:
            self._fail(
                event, "forward_reference",
                f"{event.kind} references {target_id!r} at or before its "
                "opening event; a cancel must target an earlier open",
                field_route="target_id", detail={"target_id": target_id})
        return target

    def _cancel_receipt(self, event: RawEvent, seq: int) -> None:
        target = self._resolve_cancel_target(
            event, RECEIPT, self.receipts, seq)
        released = self._release_receipt_allocations(target, event, seq)
        # Any unallocated remainder of the receipt is removed with it.
        target.status = CANCELLED
        target.cancelled_event_id = event.event_id
        target.cancelled_seq = seq
        target.remaining_cents = 0
        self.effects[event.event_id] = {
            "seq": seq, "action": "cancel_receipt",
            "object_id": target.object_id,
            "opened_allocation_ids": [],
            "released_allocation_ids": released}

    def _cancel_bill(self, event: RawEvent, seq: int) -> None:
        target = self._resolve_cancel_target(event, BILL, self.bills, seq)
        released = self._release_bill_allocations(target, event, seq)
        target.status = CANCELLED
        target.cancelled_event_id = event.event_id
        target.cancelled_seq = seq
        target.remaining_cents = 0
        self.effects[event.event_id] = {
            "seq": seq, "action": "cancel_bill",
            "object_id": target.object_id,
            "opened_allocation_ids": [],
            "released_allocation_ids": released}
        # The single post-event sweep in run() redistributes the released
        # money; the receipts keep their original opening order.

    def _release_receipt_allocations(self, receipt: _Object,
                                     event: RawEvent, seq: int) -> list[str]:
        released_ids: list[str] = []
        for allocation in self.allocations:
            if allocation.status != ACTIVE:
                continue
            if allocation.receipt_id != receipt.object_id:
                continue
            allocation.status = RELEASED
            allocation.released_event_id = event.event_id
            allocation.released_seq = seq
            allocation.release_reason = REASON_RECEIPT_CANCELLED
            bill = self.bills[allocation.bill_id]
            bill.remaining_cents += allocation.amount_cents
            released_ids.append(allocation.allocation_id)
        return released_ids

    def _release_bill_allocations(self, bill: _Object,
                                  event: RawEvent, seq: int) -> list[str]:
        released_ids: list[str] = []
        for allocation in self.allocations:
            if allocation.status != ACTIVE:
                continue
            if allocation.bill_id != bill.object_id:
                continue
            allocation.status = RELEASED
            allocation.released_event_id = event.event_id
            allocation.released_seq = seq
            allocation.release_reason = REASON_BILL_CANCELLED
            receipt = self.receipts[allocation.receipt_id]
            receipt.remaining_cents += allocation.amount_cents
            released_ids.append(allocation.allocation_id)
        return released_ids

    # -------------------------------------------------------------- sweep
    def _auto_match(self, event: RawEvent, seq: int) -> None:
        """Match earliest open bills against earliest available receipts."""
        opened: list[str] = []
        for bill in self.bills.values():
            if bill.status != OPEN or bill.remaining_cents <= 0:
                continue
            for receipt in self.receipts.values():
                if receipt.status != OPEN or receipt.remaining_cents <= 0:
                    continue
                amount = min(bill.remaining_cents, receipt.remaining_cents)
                if amount <= 0:
                    continue
                self._allocation_counter += 1
                allocation = _Allocation(
                    allocation_id=f"A{self._allocation_counter:06d}",
                    bill_id=bill.object_id, receipt_id=receipt.object_id,
                    amount_cents=amount, opened_event_id=event.event_id,
                    opened_seq=seq)
                self.allocations.append(allocation)
                bill.remaining_cents -= amount
                receipt.remaining_cents -= amount
                opened.append(allocation.allocation_id)
                if bill.remaining_cents == 0:
                    break
        effect = self.effects.get(event.event_id)
        if effect is not None:
            effect["opened_allocation_ids"].extend(opened)

    # ------------------------------------------------------------- output
    def object_summaries(self) -> list[dict]:
        summaries = []
        for registry, kind in ((self.bills, BILL), (self.receipts, RECEIPT)):
            for obj in registry.values():
                summaries.append({
                    "kind": kind,
                    "object_id": obj.object_id,
                    "status": obj.status,
                    "original_cents": obj.original_cents,
                    "original_amount": format_cents(obj.original_cents),
                    "remaining_cents": obj.remaining_cents,
                    "remaining_amount": format_cents(obj.remaining_cents),
                    "opening_event_id": obj.opening_event_id,
                    "opening_at_utc": obj.opening_at_utc.isoformat(),
                    "opening_seq": obj.opening_seq,
                    "cancelled_event_id": obj.cancelled_event_id,
                    "cancelled_seq": obj.cancelled_seq,
                    **({"due_on": obj.due_on.isoformat()}
                       if kind == BILL else {}),
                })
        return summaries

    def allocation_dicts(self) -> list[dict]:
        return [{
            "allocation_id": a.allocation_id,
            "bill_id": a.bill_id,
            "receipt_id": a.receipt_id,
            "amount_cents": a.amount_cents,
            "amount": format_cents(a.amount_cents),
            "status": a.status,
            "opened_event_id": a.opened_event_id,
            "opened_seq": a.opened_seq,
            "released_event_id": a.released_event_id,
            "released_seq": a.released_seq,
            "release_reason": a.release_reason,
        } for a in self.allocations]

    def active_allocations(self) -> list[_Allocation]:
        return [a for a in self.allocations if a.status == ACTIVE]

    def conservation(self) -> dict:
        """The two invariants that must hold at the cutoff instant."""
        open_bills = [b for b in self.bills.values()
                      if b.status == OPEN]
        open_receipts = [r for r in self.receipts.values()
                         if r.status == OPEN]
        bill_original = sum(b.original_cents for b in open_bills)
        bill_remaining = sum(b.remaining_cents for b in open_bills)
        receipt_original = sum(r.original_cents for r in open_receipts)
        receipt_remaining = sum(r.remaining_cents for r in open_receipts)
        active_total = sum(a.amount_cents for a in self.active_allocations())
        return {
            "bill_original_cents": bill_original,
            "bill_open_cents": bill_remaining,
            "receipt_original_cents": receipt_original,
            "receipt_unallocated_cents": receipt_remaining,
            "active_allocation_cents": active_total,
            "bill_invariant_ok":
                bill_original == bill_remaining + active_total,
            "receipt_invariant_ok":
                receipt_original == receipt_remaining + active_total,
        }

    def aging(self, base_day) -> dict:
        """Bucket the *currently open* part of every uncancelled bill."""
        buckets = {name: 0 for name in AGING_BUCKETS}
        details = []
        for bill in self.bills.values():
            if bill.status != OPEN:
                continue
            days = age_in_days(base_day, bill.due_on)
            bucket = age_bucket(days)
            buckets[bucket] += bill.remaining_cents
            details.append({
                "bill_id": bill.object_id,
                "due_on": bill.due_on.isoformat(),
                "age_days": days,
                "bucket": bucket,
                "open_cents": bill.remaining_cents,
                "open_amount": format_cents(bill.remaining_cents),
            })
        details.sort(key=lambda row: (row["age_days"], row["bill_id"]))
        return {
            "base_day": base_day.isoformat(),
            "bucket_cents": dict(buckets),
            "bucket_amounts": {name: format_cents(cents)
                               for name, cents in buckets.items()},
            "details": details,
        }
