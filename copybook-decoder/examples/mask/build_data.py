#!/usr/bin/env python3
"""Build the hand-constructed multi-file inputs for the mask scenarios.

The records imitate exactly what ``copybook decode``'s ``write_jsonl``
produces (``meta`` + ``fields``), but every byte is invented for this
demonstration -- no real customer data is involved.  Three files model
the classic join story:

* ``accounts.jsonl``  -- one row per account master (ACCT layout);
* ``txns.jsonl``      -- account transaction details (TXN layout);
* ``related.jsonl``   -- the related holder table (HOLDER layout).

The same account number appears in all three files so the scenarios can
prove stable cross-file replacement; nulls, missing fields and a
policy-driven rejection are built in on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _rec(source: str, line: int, layout: str, fields: dict) -> dict:
    return {
        "meta": {
            "source_file": source,
            "line_number": line,
            "byte_start": (line - 1) * 121,
            "byte_end": (line - 1) * 121 + 120,
            "layout": layout,
            "layout_version": "1",
        },
        "fields": fields,
    }


ACCOUNTS = [
    _rec("/landing/accounts.dat", 1, "ACCT", {
        "ACCT-NO": "1000002345", "CUST-NAME": "ZHANG WEI",
        "BALANCE": "12345.67", "OPEN-DATE": "2024-01-15",
        "BRANCH-CODE": "BR01", "ALT-ACCT": "1000002345"}),
    _rec("/landing/accounts.dat", 2, "ACCT", {
        "ACCT-NO": "1000002346", "CUST-NAME": "LI NA",
        "BALANCE": "999.99", "OPEN-DATE": "2024-01-15",
        "BRANCH-CODE": "BR02", "ALT-ACCT": "1000002346"}),
    # null name: kept as null (on_null defaults to keep)
    _rec("/landing/accounts.dat", 3, "ACCT", {
        "ACCT-NO": "1000002345", "CUST-NAME": None,
        "BALANCE": "250000.00", "OPEN-DATE": "2023-12-31",
        "BRANCH-CODE": "BR01", "ALT-ACCT": "1000002345"}),
    # missing CUST-NAME and ALT-ACCT entirely: different from null
    _rec("/landing/accounts.dat", 4, "ACCT", {
        "ACCT-NO": "1000002347", "BALANCE": "5000.00",
        "OPEN-DATE": "2024-02-01", "BRANCH-CODE": "BR03"}),
    # ALT-ACCT null with on_null=reject: this record is refused
    _rec("/landing/accounts.dat", 5, "ACCT", {
        "ACCT-NO": "1000002348", "CUST-NAME": "WANG FANG",
        "BALANCE": "100.00", "OPEN-DATE": "2024-03-05",
        "BRANCH-CODE": "BR02", "ALT-ACCT": None}),
]

TXNS = [
    _rec("/landing/txns.dat", 1, "TXN", {
        "ACCT-NO": "1000002345", "TXN-AMOUNT": "120.50",
        "TXN-DATE": "2024-01-15"}),
    _rec("/landing/txns.dat", 2, "TXN", {
        "ACCT-NO": "1000002345", "TXN-AMOUNT": "-30.00",
        "TXN-DATE": "2024-01-16"}),
    _rec("/landing/txns.dat", 3, "TXN", {
        "ACCT-NO": "1000002346", "TXN-AMOUNT": "1000000.00",
        "TXN-DATE": "2024-01-15"}),
    # an account with no master row: still mapped stably
    _rec("/landing/txns.dat", 4, "TXN", {
        "ACCT-NO": "1000002999", "TXN-AMOUNT": "0.01",
        "TXN-DATE": "2024-02-01"}),
]

RELATED = [
    _rec("/landing/related.dat", 1, "HOLDER", {
        "ACCT-NO": "1000002345", "HOLDER-NAME": "ZHANG WEI",
        "HOLDER-SINCE": "2024-01-15"}),
    _rec("/landing/related.dat", 2, "HOLDER", {
        "ACCT-NO": "1000002346", "HOLDER-NAME": "LI NA",
        "HOLDER-SINCE": "2022-06-30"}),
]


def _write(path: Path, rows: list) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def build_all(root: Path) -> dict:
    """Materialise every scenario input under *root*; return the paths."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write(data / "accounts.jsonl", ACCOUNTS)
    _write(data / "txns.jsonl", TXNS)
    _write(data / "related.jsonl", RELATED)

    # corrupt input: one truncated JSON line among valid ones
    corrupt = root / "corrupt"
    corrupt.mkdir(parents=True, exist_ok=True)
    good = json.dumps(TXNS[0])
    (corrupt / "txns.jsonl").write_text(
        good + "\n" + good[:-20] + "\n", encoding="utf-8")
    _write(corrupt / "accounts.jsonl", ACCOUNTS[:1])
    _write(corrupt / "related.jsonl", RELATED)

    return {"data": data, "corrupt": corrupt}


if __name__ == "__main__":
    paths = build_all(HERE / "run")
    for name, path in paths.items():
        print(f"{name}: {path}")
