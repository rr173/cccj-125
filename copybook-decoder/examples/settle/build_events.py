#!/usr/bin/env python3
"""Generate the settlement example event files.

The generated data is ordinary decoded JSONL (meta/fields wrapper) split
over several files, exactly like the decoder's ``write_jsonl`` output.
Every expectation checked by run_scenarios.py is derived by hand in
expected_results.py; this builder never imports the settlement processor.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

CONFIG = {
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


def ev(eid, kind, at, *, account="ACC1", currency="CNY", oid=None,
       amt=None, due=None, tid=None, source="events.jsonl", line=1):
    fields = {"ID": eid, "ACCT": account, "CCY": currency,
              "AT": at, "KIND": kind}
    if oid is not None:
        fields["OID"] = oid
    if amt is not None:
        fields["AMT"] = amt
    if due is not None:
        fields["DUE"] = due
    if tid is not None:
        fields["TID"] = tid
    return {"meta": {"source_file": source, "line_number": line},
            "fields": fields}


def write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def hand_events():
    """The nine-event hand scenario."""
    d = "2026-01-01T00:00:"
    return [
        ev("e01", "BILL", d + "01Z", oid="A", amt="100.00", due="2026-01-01"),
        ev("e02", "BILL", d + "02Z", oid="B", amt="100.00", due="2026-01-01"),
        ev("e03", "RECEIPT", d + "03Z", oid="P", amt="150.00"),
        ev("e04", "RECEIPT", d + "04Z", oid="Q", amt="50.00"),
        ev("e05", "CANCEL_RECEIPT", d + "05Z", tid="P"),
        ev("e06", "BILL", d + "06Z", oid="C", amt="100.00", due="2026-01-01"),
        ev("e07", "RECEIPT", d + "07Z", oid="R", amt="60.00"),
        ev("e08", "CANCEL_BILL", d + "08Z", tid="B"),
        ev("e09", "CANCEL_RECEIPT", d + "09Z", tid="Q"),
    ]


def build_all(root: Path):
    if root.exists():
        shutil.rmtree(root)
    data = root / "data"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "settle_config.json").write_text(
        json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1. Hand scenario, single ordered file.
    write(data / "hand" / "events.jsonl", hand_events())
    # 1b. Same events split into two files and shuffled; order must not
    #     change the destination.
    rows = hand_events()
    shuffled = [rows[i] for i in (8, 0, 5, 2, 7, 1, 4, 6, 3)]
    part0, part1 = shuffled[:5], shuffled[5:]
    for n, part in enumerate((part0, part1)):
        for line, row in enumerate(part, start=1):
            row["meta"]["source_file"] = f"part{n}.jsonl"
            row["meta"]["line_number"] = line
        write(data / "hand_shuffled" / f"part{n}.jsonl", part)

    # 2. Early receipt, bill only appears days later.
    write(data / "early_receipt" / "events.jsonl", [
        ev("p1", "RECEIPT", "2026-01-01T00:00:00Z", oid="P", amt="100.00"),
        ev("b1", "BILL", "2026-01-05T00:00:00Z", oid="B", amt="100.00",
           due="2026-02-01"),
    ])

    # 3. Same instant: UTF-8 byte order of event_id decides.
    t = "2026-01-01T00:00:00Z"
    write(data / "same_instant" / "events.jsonl", [
        ev("P2", "RECEIPT", t, oid="P2", amt="100.00"),
        ev("B2", "BILL", t, oid="B2", amt="100.00", due="2026-02-01"),
        ev("P1", "RECEIPT", t, oid="P1", amt="100.00"),
        ev("B1", "BILL", t, oid="B1", amt="100.00", due="2026-02-01"),
    ])

    # 4. Non-UTC offsets that normalize to the same UTC instant.
    write(data / "offsets" / "events.jsonl", [
        ev("B1", "BILL", "2026-01-01T08:00:00+08:00", oid="B1",
           amt="10.00", due="2026-02-01"),
        ev("P1", "RECEIPT", "2026-01-01T03:00:00+03:00", oid="P1",
           amt="10.00"),
    ])

    # 5. Cutoff exactly equal to the event instant.
    write(data / "cutoff_equal" / "events.jsonl", [
        ev("B1", "BILL", "2026-03-01T12:00:00Z", oid="B1", amt="10.00",
           due="2026-03-01"),
        ev("P1", "RECEIPT", "2026-03-01T12:00:00Z", oid="P1", amt="10.00"),
    ])

    # 6. Every aging bucket at its due-date boundary (fully open bills).
    boundary = [
        ("B-FUT", "2026-04-01"), ("B-D0", "2026-03-31"),
        ("B-D30", "2026-03-01"), ("B-D31", "2026-02-28"),
        ("B-D61", "2026-01-29"), ("B-D91", "2025-12-30"),
    ]
    write(data / "aging" / "events.jsonl", [
        ev(eid, "BILL", f"2026-01-01T00:00:{i:02d}Z", oid=eid,
           amt="100.00", due=due)
        for i, (eid, due) in enumerate(boundary, start=1)])

    # 7. Unallocated money: receipts exceed the only bill.
    write(data / "unallocated" / "events.jsonl", [
        ev("P1", "RECEIPT", "2026-01-01T00:00:00Z", oid="P1",
           amt="100.00"),
        ev("P2", "RECEIPT", "2026-01-01T00:00:01Z", oid="P2",
           amt="25.50"),
        ev("B1", "BILL", "2026-01-02T00:00:00Z", oid="B1", amt="100.00",
           due="2026-02-01"),
    ])

    # 8. Amount far beyond a signed 64-bit integer.
    huge = "9" * 30 + ".00"
    write(data / "huge" / "events.jsonl", [
        ev("B1", "BILL", "2026-01-01T00:00:00Z", oid="B1", amt=huge,
           due="2026-02-01"),
        ev("P1", "RECEIPT", "2026-01-01T00:00:01Z", oid="P1", amt=huge),
    ])

    # 9. One good group and one bad group whose fault is its LAST event:
    #    a double cancel, a back reference and an end-of-group error.
    write(data / "bad_group" / "events.jsonl", [
        ev("g1", "BILL", "2026-01-01T00:00:01Z", account="GOOD", oid="GB",
           amt="10.00", due="2026-02-01"),
        ev("g2", "RECEIPT", "2026-01-01T00:00:02Z", account="GOOD",
           oid="GP", amt="10.00"),
        ev("b1", "RECEIPT", "2026-01-01T00:00:01Z", account="BAD",
           oid="XP", amt="10.00"),
        ev("b2", "CANCEL_RECEIPT", "2026-01-01T00:00:02Z", account="BAD",
           tid="XP"),
        ev("b3", "CANCEL_RECEIPT", "2026-01-01T00:00:03Z", account="BAD",
           tid="XP"),     # double cancel, last event of the group
    ])

    # 10. Fatal inputs: malformed JSON and a duplicate event_id.
    cdir = data / "fatal"
    write(cdir / "corrupt.jsonl", [
        ev("ok1", "RECEIPT", "2026-01-01T00:00:01Z", oid="P", amt="1.00")])
    with (cdir / "corrupt.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    write(cdir / "duplicate.jsonl", [
        ev("DUP", "RECEIPT", "2026-01-01T00:00:01Z", oid="P1", amt="1.00"),
        ev("DUP", "BILL", "2026-01-01T00:00:02Z", oid="B1", amt="1.00",
           due="2026-02-01"),
    ])
    return root


if __name__ == "__main__":
    build_all(HERE / "run")
    print("settlement example data generated under", HERE / "run")
