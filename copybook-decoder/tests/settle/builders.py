"""Test fixtures for the open-item settlement processor."""

import json
from pathlib import Path

# Default field routes; tests override individual values where needed.
DEFAULT_CONFIG = {
    "paths": {
        "event_id": ["ID"],
        "account": ["ACCT"],
        "currency": ["CCY"],
        "at": ["AT"],
        "kind": ["KIND"],
        "object_id": ["OID"],
        "amount": ["AMT"],
        "due_on": ["DUE"],
        "target_id": ["TID"],
    }}


def event(eid, kind, at, *, account="ACC1", currency="CNY",
          object_id=None, amount=None, due_on=None, target_id=None,
          source_file="events.jsonl", line_number=1, extra_fields=None):
    """Build one meta/fields-wrapped event as decoded JSONL would carry."""
    fields = {"ID": eid, "ACCT": account, "CCY": currency,
              "AT": at, "KIND": kind}
    if object_id is not None:
        fields["OID"] = object_id
    if amount is not None:
        fields["AMT"] = amount
    if due_on is not None:
        fields["DUE"] = due_on
    if target_id is not None:
        fields["TID"] = target_id
    if extra_fields:
        fields.update(extra_fields)
    return {
        "meta": {"source_file": source_file, "line_number": line_number},
        "fields": fields,
    }


def bill(eid, at, object_id, amount, due_on, **kwargs):
    return event(eid, "BILL", at, object_id=object_id, amount=amount,
                 due_on=due_on, **kwargs)


def receipt(eid, at, object_id, amount, **kwargs):
    return event(eid, "RECEIPT", at, object_id=object_id, amount=amount,
                 **kwargs)


def cancel_bill(eid, at, target_id, **kwargs):
    return event(eid, "CANCEL_BILL", at, target_id=target_id, **kwargs)


def cancel_receipt(eid, at, target_id, **kwargs):
    return event(eid, "CANCEL_RECEIPT", at, target_id=target_id, **kwargs)


def write_jsonl(path, rows):
    path = Path(path)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def split_files(directory, rows, sizes):
    """Split *rows* into several JSONL files with the given row counts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    index = 0
    for n, size in enumerate(sizes):
        chunk = rows[index:index + size]
        index += size
        for offset, row in enumerate(chunk, start=1):
            row["meta"]["source_file"] = f"part{n}.jsonl"
            row["meta"]["line_number"] = offset
        paths.append(write_jsonl(Path(directory) / f"part{n}.jsonl", chunk))
    assert index == len(rows)
    return paths
