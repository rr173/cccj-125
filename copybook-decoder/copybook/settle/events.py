"""Loading and routing validation of settlement event JSONL files.

Every input line keeps the exact ``meta`` / ``fields`` wrapper it arrived
with plus its source file and physical line number, so every later
diagnostic and every delivered fact can be traced back to the origin.

The loader enforces only the *routing* level here:

* each line is one JSON object with ``meta`` and ``fields`` objects;
* ``event_id`` is a non-empty string unique over the **whole** input set,
  including events that fall after the requested cutoff;
* ``account`` and ``currency`` are non-empty strings and the currency is
  one of the supported codes;
* ``at`` is a second-level ISO 8601 timestamp with an explicit offset;
* ``kind`` is one of the four known event kinds.

Any failure at this level makes the whole run impossible -- the processor
cannot even decide which group an event belongs to -- so it raises
:class:`SettleInputError` and no formal result is produced.  Business-field
validation (amounts, dates, object references) happens later, inside the
per-(account, currency) group, where errors can be contained.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .config import SettleConfig
from .errors import SettleInputError
from .values import parse_timestamp

BILL = "BILL"
RECEIPT = "RECEIPT"
CANCEL_BILL = "CANCEL_BILL"
CANCEL_RECEIPT = "CANCEL_RECEIPT"

#: The four legal event kinds.
EVENT_KINDS = (BILL, RECEIPT, CANCEL_BILL, CANCEL_RECEIPT)

#: Cancellation kinds mapped to the object kind they target.
CANCEL_TARGET_KIND = {CANCEL_BILL: BILL, CANCEL_RECEIPT: RECEIPT}

#: The two supported currencies; money is always integer cents and the two
#: currencies are never mixed (they also form distinct groups).
CURRENCIES = ("CNY", "USD")


@dataclass
class RawEvent:
    """One routed input event, kept in its original wrapper for traceback."""

    event_id: str
    account: str
    currency: str
    kind: str
    at_utc: Any                      # timezone-aware datetime, normalized to UTC
    at_text: str                     # original timestamp spelling
    fields: dict
    meta: Any
    raw: dict                        # the original JSON object, verbatim
    source_file: str
    line_number: int
    input_order: int                 # stable (file, line) reading ordinal


def _where(source_file: str, line_number: Optional[int]) -> str:
    if line_number is None:
        return str(source_file)
    return f"{source_file} line {line_number}"


def load_events(inputs, *, config: SettleConfig) -> list[RawEvent]:
    """Read every JSONL input, validate routing, return canonical order.

    The returned list is ordered by ``(at in UTC, event_id UTF-8 bytes)``,
    so file arrangement and chunking can never change the result.  Input
    paths are resolved to absolute strings purely for stable diagnostics.
    """
    events: list[RawEvent] = []
    seen_ids: dict[str, str] = {}
    input_order = 0

    for input_path in inputs:
        path = Path(input_path)
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            raise SettleInputError(
                f"cannot open event file {path}: {exc}")
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise SettleInputError(
                        f"{_where(str(path), line_number)}: blank lines are "
                        "not legal event records")
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SettleInputError(
                        f"{_where(str(path), line_number)}: invalid JSON: "
                        f"{exc}")
                input_order += 1
                event = _route_event(obj, str(path), line_number,
                                     input_order, config)
                previous = seen_ids.get(event.event_id)
                if previous is not None:
                    raise SettleInputError(
                        f"{_where(event.source_file, event.line_number)}: "
                        f"duplicate event_id {event.event_id!r}; first seen "
                        f"at {previous}; event_id must be unique across the "
                        "whole input")
                seen_ids[event.event_id] = _where(event.source_file,
                                                   event.line_number)
                events.append(event)

    # Canonical ordering: UTC instant first, then the event_id's UTF-8 byte
    # sequence (not a locale-dependent string comparison).
    events.sort(key=lambda ev: (ev.at_utc, ev.event_id.encode("utf-8")))
    return events


def _route_event(obj: Any, source_file: str, line_number: int,
                 input_order: int, config: SettleConfig) -> RawEvent:
    where = _where(source_file, line_number)
    if not isinstance(obj, dict):
        raise SettleInputError(f"{where}: each event must be a JSON object")
    meta = obj.get("meta")
    fields = obj.get("fields")
    if not isinstance(meta, dict) or not isinstance(fields, dict):
        raise SettleInputError(
            f"{where}: events need 'meta' and 'fields' objects")

    def text_route(route: str) -> str:
        value = config.get(route).get(fields)
        if not isinstance(value, str) or not value:
            raise SettleInputError(
                f"{where}: route '{route}' must resolve to a non-empty "
                f"string, got {value!r}")
        return value

    event_id = text_route("event_id")
    account = text_route("account")
    currency = text_route("currency")
    if currency not in CURRENCIES:
        raise SettleInputError(
            f"{where}: currency {currency!r} is not one of "
            f"{', '.join(CURRENCIES)}")
    at_text = text_route("at")
    try:
        at_utc = parse_timestamp(at_text)
    except ValueError as exc:
        raise SettleInputError(f"{where}: {exc}")
    kind = text_route("kind")
    if kind not in EVENT_KINDS:
        raise SettleInputError(
            f"{where}: kind {kind!r} is not one of "
            f"{', '.join(EVENT_KINDS)}")

    return RawEvent(
        event_id=event_id, account=account, currency=currency, kind=kind,
        at_utc=at_utc, at_text=at_text, fields=fields, meta=meta, raw=obj,
        source_file=source_file, line_number=line_number,
        input_order=input_order)
