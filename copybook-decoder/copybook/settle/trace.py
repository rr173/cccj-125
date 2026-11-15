"""Object tracing over a delivered settlement result.

A fresh process -- with none of the original input files -- can answer
"where did the money of bill/receipt ``X`` go?" by reading only the files
in a settlement output directory.  :func:`trace_object` joins the object
row, every event that mentions the id (as an opened object or a cancel
target) and the full allocation history of every edge touching it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from .engine import (ALLOCATIONS_JSONL, EVENTS_JSONL, INVALID_GROUPS_JSONL,
                     OBJECTS_JSONL, REPORT_JSON)
from .errors import SettleInputError, SettleQueryError
from .events import BILL, RECEIPT


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SettleInputError(
                    f"{path} line {line_number} is not valid JSON: {exc}")
    return rows


def trace_object(result_dir: Union[str, Path], object_id: str, *,
                 kind: Optional[str] = None) -> dict:
    """Trace one bill or receipt through a delivered settlement directory.

    *kind* may be ``BILL`` or ``RECEIPT``; when omitted and the id exists
    in both namespaces both traces are returned in ``matches``.  Raises
    :class:`SettleQueryError` when the object is unknown.
    """
    out_dir = Path(result_dir)
    report_path = out_dir / REPORT_JSON
    if not report_path.is_file():
        raise SettleQueryError(
            f"{out_dir} is not a settlement result directory "
            f"({REPORT_JSON} missing)")
    if kind is not None and kind not in (BILL, RECEIPT):
        raise SettleQueryError(
            f"kind must be {BILL} or {RECEIPT} when given, got {kind!r}")
    if not isinstance(object_id, str) or not object_id:
        raise SettleQueryError("object_id must be a non-empty string")

    objects = [row for row in _read_jsonl(out_dir / OBJECTS_JSONL)
               if row["object_id"] == object_id
               and (kind is None or row["kind"] == kind)]
    allocations = _read_jsonl(out_dir / ALLOCATIONS_JSONL)
    events = _read_jsonl(out_dir / EVENTS_JSONL)
    invalid = _read_jsonl(out_dir / INVALID_GROUPS_JSONL)

    if not objects:
        # The id may belong to a group that was invalidated before any
        # object row could be delivered; point the caller at its events.
        mentioned = [ev for ev in events
                     if ev.get("routed", {}).get("object_id") == object_id
                     or ev.get("routed", {}).get("target_id") == object_id]
        if mentioned:
            accounts = sorted({ev["account"] for ev in mentioned})
            raise SettleQueryError(
                f"object_id {object_id!r} has no delivered object row; its "
                f"group(s) {accounts} were invalidated. Inspect "
                f"{INVALID_GROUPS_JSONL} and the matching events in "
                f"{EVENTS_JSONL}")
        raise SettleQueryError(
            f"object_id {object_id!r} is not present in {out_dir}")

    matches = []
    for obj in objects:
        account, currency = obj["account"], obj["currency"]
        if kind == "BILL" or (kind is None and obj["kind"] == BILL):
            edges = [a for a in allocations
                     if a["account"] == account
                     and a["currency"] == currency
                     and a["bill_id"] == object_id]
        else:
            edges = [a for a in allocations
                     if a["account"] == account
                     and a["currency"] == currency
                     and a["receipt_id"] == object_id]
        edge_ids = {edge["allocation_id"] for edge in edges}
        related_events = []
        for ev in events:
            if ev["account"] != account or ev["currency"] != currency:
                continue
            routed = ev.get("routed", {})
            effect = ev.get("effect") or {}
            touched = (
                routed.get("object_id") == object_id
                or routed.get("target_id") == object_id
                or effect.get("object_id") == object_id
                or edge_ids.intersection(
                    effect.get("opened_allocation_ids", []))
                or edge_ids.intersection(
                    effect.get("released_allocation_ids", [])))
            if touched:
                related_events.append(ev)
        group_error = next(
            (record for record in invalid
             if record["account"] == account
             and record["currency"] == currency), None)
        matches.append({
            "object": obj,
            "related_events": [_event_ref(ev) for ev in related_events],
            "allocation_history": edges,
            "group_errors": group_error["errors"] if group_error else [],
        })

    return {
        "result_dir": str(out_dir),
        "object_id": object_id,
        "kind": kind,
        "match_count": len(matches),
        "matches": matches,
    }


def _event_ref(event: dict) -> dict:
    return {
        "event_id": event["event_id"],
        "kind": event["kind"],
        "at_utc": event["at_utc"],
        "applied": event["applied"],
        "source_file": event["source_file"],
        "line_number": event["line_number"],
        "effect": event.get("effect"),
    }
