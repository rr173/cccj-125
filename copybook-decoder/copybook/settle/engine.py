"""Settlement runner: load, group, replay, deliver.

The runner is the only component that touches the file system for output.
It groups routed events by ``(account, currency)`` -- the two currencies
never share a ledger -- replays each group independently up to the cutoff
instant, and materialises a self-describing result directory that can
explain every balance in a fresh process without any input files:

* ``settle_report.json``  cutoff, config, per-group balances, aging,
  conservation invariants, errors and global counters;
* ``events.jsonl``        every input event in canonical order with its
  origin (source file/line), original wrapper, applied flag and, for
  applied events of a valid group, its replay effect;
* ``allocations.jsonl``   the full open/release history of every edge;
* ``objects.jsonl``       original amount, current balance and status of
  every bill and receipt of every valid group;
* ``invalid_groups.jsonl`` exists only when at least one group is invalid,
  carrying that group's localized business errors.

Invalid groups never contribute balances, aging or active allocations;
their events are still retained.  The output directory is created only
after every group has been replayed and must not already exist, so a
failed run leaves no formal result behind.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Optional, Union

from .config import SettleConfig
from .errors import SettleInputError
from .events import (BILL, CANCEL_BILL, CANCEL_RECEIPT, RECEIPT, RawEvent,
                     load_events)
from .ledger import GroupProcessor
from .values import (AGING_BUCKETS, format_cents, parse_timestamp,
                     utc_calendar_day)

REPORT_JSON = "settle_report.json"
EVENTS_JSONL = "events.jsonl"
ALLOCATIONS_JSONL = "allocations.jsonl"
OBJECTS_JSONL = "objects.jsonl"
INVALID_GROUPS_JSONL = "invalid_groups.jsonl"

#: Exit-code contract used by the CLI.
EXIT_OK = 0
EXIT_BUSINESS_REJECT = 1
EXIT_CANNOT_PROCESS = 2


def _group_key(event: RawEvent) -> tuple[str, str]:
    return event.account, event.currency


def run_settlement(inputs, *, config: SettleConfig, cutoff,
                   output_dir: Union[str, Path]) -> dict:
    """Build the open-item settlement result for one cutoff instant.

    *inputs* is an iterable of JSONL file paths.  *cutoff* is an ISO 8601
    timestamp with an explicit offset; only events ``at <= cutoff`` (UTC)
    are applied.  Raises :class:`SettleInputError` for routing-level
    failures or when *output_dir* already exists.
    """
    cutoff_utc = _parse_cutoff(cutoff)
    events = load_events(inputs, config=config)
    base_day = utc_calendar_day(cutoff_utc)

    groups: dict[tuple[str, str], list[RawEvent]] = {}
    for event in events:
        groups.setdefault(_group_key(event), []).append(event)

    group_results: list[dict] = []
    invalid_records: list[dict] = []
    # Object and allocation rows are collected group by group; group order
    # is fixed by (account UTF-8 bytes, currency) for byte-stable output.
    object_rows: list[dict] = []
    allocation_rows: list[dict] = []
    event_effects: dict[str, dict] = {}
    event_group_valid: dict[str, bool] = {}

    for key in sorted(groups, key=lambda k: (k[0].encode("utf-8"), k[1])):
        account, currency = key
        group_events = groups[key]
        processor = GroupProcessor(account, currency, config)
        processor.run(group_events, cutoff_utc)
        applied_events = [ev for ev in group_events
                          if ev.at_utc <= cutoff_utc]
        for ev in group_events:
            event_group_valid[ev.event_id] = not processor.invalid
        if not processor.invalid:
            event_effects.update(processor.effects)
            conservation = processor.conservation()
            aging = processor.aging(base_day)
            for row in processor.object_summaries():
                object_rows.append({"account": account,
                                    "currency": currency, **row})
            for row in processor.allocation_dicts():
                allocation_rows.append({"account": account,
                                        "currency": currency, **row})
            active = processor.active_allocations()
            group_results.append({
                "account": account,
                "currency": currency,
                "status": "valid",
                "applied_event_count": len(applied_events),
                "post_cutoff_event_count":
                    len(group_events) - len(applied_events),
                "bill_count": sum(
                    1 for o in processor.object_summaries()
                    if o["kind"] == BILL),
                "receipt_count": sum(
                    1 for o in processor.object_summaries()
                    if o["kind"] == RECEIPT),
                "active_allocation_count": len(active),
                "conservation": conservation,
                "aging": aging,
                "errors": [],
            })
        else:
            invalid_records.append({
                "account": account,
                "currency": currency,
                "status": "invalid",
                "applied_event_count": len(applied_events),
                "post_cutoff_event_count":
                    len(group_events) - len(applied_events),
                "errors": [e.as_dict() for e in processor.errors],
            })
            group_results.append({
                "account": account,
                "currency": currency,
                "status": "invalid",
                "applied_event_count": len(applied_events),
                "post_cutoff_event_count":
                    len(group_events) - len(applied_events),
                "errors": [e.as_dict() for e in processor.errors],
            })

    out_dir = Path(output_dir)
    file_manifest = {
        "report": REPORT_JSON,
        "events": EVENTS_JSONL,
        "objects": OBJECTS_JSONL,
        "allocations": ALLOCATIONS_JSONL,
        **({"invalid_groups": INVALID_GROUPS_JSONL}
           if invalid_records else {}),
    }
    summary = _build_summary(
        config=config, cutoff_text=cutoff, cutoff_utc=cutoff_utc,
        base_day=base_day, inputs=inputs, events=events,
        group_results=group_results, invalid_records=invalid_records,
        file_manifest=file_manifest, output_dir=out_dir)

    # The delivery path is reserved only after every group has replayed;
    # a failed run never creates the directory or a partial result.
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SettleInputError(
            f"output directory {out_dir} already exists; refusing to "
            "overwrite an existing settlement result")

    _write_report(out_dir / REPORT_JSON, summary)
    _write_events(out_dir / EVENTS_JSONL, events, config, cutoff_utc,
                  event_effects, event_group_valid)
    _write_jsonl(out_dir / OBJECTS_JSONL, object_rows)
    _write_jsonl(out_dir / ALLOCATIONS_JSONL, allocation_rows)
    if invalid_records:
        _write_jsonl(out_dir / INVALID_GROUPS_JSONL, invalid_records)

    summary["files"] = file_manifest
    return summary


def _parse_cutoff(cutoff) -> "object":
    if not isinstance(cutoff, str):
        raise SettleInputError(
            f"cutoff must be an ISO 8601 offset timestamp string, got "
            f"{cutoff!r}")
    try:
        return parse_timestamp(cutoff)
    except ValueError as exc:
        raise SettleInputError(f"invalid cutoff: {exc}")


def _build_summary(*, config: SettleConfig, cutoff_text: str, cutoff_utc,
                   base_day, inputs, events, group_results,
                   invalid_records, file_manifest, output_dir) -> dict:
    valid_groups = [g for g in group_results if g["status"] == "valid"]
    bucket_totals: dict[tuple[str, str], dict[str, int]] = {}
    for group in valid_groups:
        bucket_totals[(group["account"], group["currency"])] = {
            name: group["aging"]["bucket_cents"][name]
            for name in AGING_BUCKETS}
    return {
        "tool": "copybook-settle",
        "schema_version": 1,
        "output_dir": str(output_dir),
        "files": file_manifest,
        "cutoff": {
            "requested": cutoff_text,
            "utc": cutoff_utc.astimezone(timezone.utc).isoformat(),
            "base_day": base_day.isoformat(),
        },
        "config_paths": {name: config.get(name).spec()
                         for name in config.paths},
        "inputs": [str(Path(p)) for p in inputs],
        "event_count": len(events),
        "applied_event_count": sum(g["applied_event_count"]
                                   for g in group_results),
        "post_cutoff_event_count": sum(g["post_cutoff_event_count"]
                                       for g in group_results),
        "group_count": len(group_results),
        "valid_group_count": len(valid_groups),
        "invalid_group_count": len(invalid_records),
        "conclusion": "business_rejected" if invalid_records else "settled",
        "totals_by_currency": _totals_by_currency(valid_groups),
        "aging_buckets": {
            f"{account}|{currency}": {
                name: {"cents": cents, "amount": format_cents(cents)}
                for name, cents in buckets.items()}
            for (account, currency), buckets in sorted(bucket_totals.items())},
        "groups": group_results,
    }


def _totals_by_currency(valid_groups: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for group in valid_groups:
        currency = group["currency"]
        block = out.setdefault(currency, {key: 0 for key in (
            "bill_original_cents", "bill_open_cents",
            "receipt_original_cents", "receipt_unallocated_cents",
            "active_allocation_cents")})
        for key in block:
            block[key] += group["conservation"][key]
    for block in out.values():
        block.update({
            "bill_open_amount": format_cents(block["bill_open_cents"]),
            "receipt_unallocated_amount":
                format_cents(block["receipt_unallocated_cents"]),
            "active_allocation_amount":
                format_cents(block["active_allocation_cents"]),
        })
    return out


def _write_report(path: Path, summary: dict) -> None:
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=False)
        + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_events(path: Path, events: list[RawEvent], config: SettleConfig,
                  cutoff_utc, effects: dict, group_valid: dict) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            applied = event.at_utc <= cutoff_utc
            row = {
                "event_id": event.event_id,
                "account": event.account,
                "currency": event.currency,
                "kind": event.kind,
                "at": event.at_text,
                "at_utc": event.at_utc.isoformat(),
                "applied": applied,
                "group_valid": group_valid.get(event.event_id),
                "source_file": event.source_file,
                "line_number": event.line_number,
                "effect": effects.get(event.event_id),
                "routed": {
                    route: config.get(route).get(event.fields)
                    for route in config.paths},
                "meta": event.meta,
                "fields": event.fields,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
