#!/usr/bin/env python3
"""Build the relational-admission handoff scenario.

The scenario starts from the SAME real decoder used everywhere else in the
project: an EBCDIC account feed (examples/data/BANKA-20240315.dat) is
decoded with the versioned copybook into the standard meta/fields JSONL,
and those decoded rows become one of the admission input tables.  The
remaining tables (transactions, branches and an empty terminal table) are
written in the identical wrapper shape, so the admission job never needs
to re-decode anything.

Layout of the handoff::

    accounts (real decode output)
      candidate key k_acct on ACCT-ID
    branches
      candidate key k_branch on BRANCH-ID
    transactions (two files, one logical table)
      r_acct   (TXN-ACCT)            -> accounts.k_acct   mandatory
      r_branch (TXN-BRANCH, REGION)  -> branches.k_branch optional
    terminated (empty table)
      accounts additionally carries r_gone -> terminated (mandatory),
      which is the deliberately broken reference that drives the cascade.

Running build_scenarios.py only (re)writes inputs; run_scenarios.py runs
the admission job and prints/asserts the expected verdicts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook import Manifest
from copybook.pipeline import decode_file

WORK = HERE / "run" / "input"


def _record(fields: dict, source_file: str, line_number: int,
            byte_start: int, byte_end: int) -> dict:
    return {
        "meta": {"source_file": source_file, "line_number": line_number,
                 "byte_start": byte_start, "byte_end": byte_end},
        "fields": fields,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def decode_real_accounts() -> Path:
    """Run the real decoder and keep its JSONL as the accounts table."""
    manifest = Manifest.load(PROJECT / "examples" / "manifest.json")
    result = decode_file(
        PROJECT / "examples" / "data" / "BANKA-20240315.dat",
        sender="BANKA", business_date="2024-03-15", manifest=manifest)
    out = WORK / "accounts.jsonl"
    result.write_jsonl(out)
    # Annotate the surviving rows with the extra columns the contract
    # declares; the original payload stays byte-for-byte otherwise.
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    extra = {
        "0000000001": {"REGION": "CN", "BRANCH-ID": "BR1",
                      "GONE-KEY": "TERM-1"},
        "0000000002": {"REGION": "CN", "BRANCH-ID": "BR2",
                      "GONE-KEY": "TERM-1"},
        "0000000003": {"REGION": "EU", "BRANCH-ID": "BR3",
                      "GONE-KEY": "GHOST"},
    }
    for row in rows:
        row["fields"].update(extra[row["fields"]["ACCT-ID"]])
    _write(out, rows)
    return out


def build_branches() -> Path:
    rows = [
        # BR1 is the self-supporting root: its parent points at itself.
        _record({"BRANCH-ID": "BR1", "REGION": "CN", "NAME": "BEIJING",
                 "PARENT": "BR1"},
                "branches.jsonl", 1, 0, 40),
        _record({"BRANCH-ID": "BR2", "REGION": "CN", "NAME": "SHANGHAI",
                 "PARENT": "BR1"},
                "branches.jsonl", 2, 41, 81),
        # BR3's key tuple is fine, but its mandatory self reference names a
        # branch that no row carries -> locally valid but unsupported
        # (layer 1), which then cascades to dependent transactions.
        _record({"BRANCH-ID": "BR3", "REGION": "EU", "NAME": "PARIS",
                 "PARENT": "NO-SUCH-BRANCH"},
                "branches.jsonl", 3, 82, 122),
    ]
    out = WORK / "branches.jsonl"
    _write(out, rows)
    return out


def build_transactions() -> list[Path]:
    # One logical table spread over two physical files.
    part1 = [
        _record({"TXN-ID": "T001", "TXN-ACCT": "0000000001",
                 "TXN-BRANCH": "BR1", "REGION": "CN", "AMOUNT": 100},
                "txns.part1.jsonl", 1, 0, 60),
        # Account id "0000000099" does not exist: layer-1 transaction.
        _record({"TXN-ID": "T002", "TXN-ACCT": "0000000099",
                 "TXN-BRANCH": "BR1", "REGION": "CN", "AMOUNT": 7},
                "txns.part1.jsonl", 2, 61, 121),
    ]
    part2 = [
        # Points at account 0000000003, whose r_gone reference is broken:
        # the transaction survives only while that account survives.
        _record({"TXN-ID": "T003", "TXN-ACCT": "0000000003",
                 "TXN-BRANCH": "BR3", "REGION": "EU", "AMOUNT": -25},
                "txns.part2.jsonl", 1, 0, 60),
        # Optional composite reference that is fully null -> exempt.
        _record({"TXN-ID": "T004", "TXN-ACCT": "0000000002",
                 "TXN-BRANCH": None, "REGION": None, "AMOUNT": 9},
                "txns.part2.jsonl", 2, 61, 121),
    ]
    p1 = WORK / "txns.part1.jsonl"
    p2 = WORK / "txns.part2.jsonl"
    _write(p1, part1)
    _write(p2, part2)
    return [p1, p2]


def build_terminated() -> Path:
    # A real terminated account supports the two healthy accounts; account
    # 3 points at "GHOST", which this table cannot provide.
    out = WORK / "terminated.jsonl"
    rows = [
        _record({"TERM-ID": "TERM-1"}, "terminated.jsonl", 1, 0, 40),
    ]
    _write(out, rows)
    return out


def build_contract(paths: dict[str, Path]) -> Path:
    contract = {
        "tables": [
            {
                "name": "accounts",
                "files": [str(paths["accounts"])],
                "row_id_path": ["ACCT-ID"],
                "fields": {
                    "acct_id": {"path": ["ACCT-ID"], "type": "string"},
                    "name": {"path": ["ACCT-NAME"], "type": "string"},
                    "balance": {"path": ["BALANCE"], "type": "string"},
                    "txn_date": {"path": ["LAST-TXN-DATE"],
                                 "type": "integer"},
                    "region": {"path": ["REGION"], "type": "string",
                               "nullable": True},
                    "branch_id": {"path": ["BRANCH-ID"],
                                  "type": "string", "nullable": True},
                    "gone_key": {"path": ["GONE-KEY"], "type": "string",
                                 "nullable": True}},
                "candidate_keys": {"k_acct": ["acct_id"]},
                "references": [
                    {"name": "r_gone", "columns": ["gone_key"],
                     "target_table": "terminated", "target_key": "k_term",
                     "rule": "mandatory"}],
            },
            {
                "name": "branches",
                "files": [str(paths["branches"])],
                "row_id_path": ["BRANCH-ID"],
                "fields": {
                    "branch_id": {"path": ["BRANCH-ID"],
                                  "type": "string"},
                    "region": {"path": ["REGION"], "type": "string"},
                    "name": {"path": ["NAME"], "type": "string"},
                    "parent": {"path": ["PARENT"], "type": "string",
                               "nullable": True}},
                "candidate_keys": {
                    "k_branch": ["branch_id", "region"],
                    "k_branch_s": ["branch_id"]},
                "references": [
                    {"name": "r_parent", "columns": ["parent"],
                     "target_table": "branches", "target_key": "k_branch_s",
                     "rule": "mandatory"}],
            },
            {
                "name": "transactions",
                "files": [str(paths["txns.part1"]),
                          str(paths["txns.part2"])],
                "row_id_path": ["TXN-ID"],
                "fields": {
                    "txn_id": {"path": ["TXN-ID"], "type": "string"},
                    "acct": {"path": ["TXN-ACCT"], "type": "string"},
                    "branch": {"path": ["TXN-BRANCH"], "type": "string",
                               "nullable": True},
                    "region": {"path": ["REGION"], "type": "string",
                               "nullable": True},
                    "amount": {"path": ["AMOUNT"], "type": "integer"}},
                "candidate_keys": {"k_txn": ["txn_id"]},
                "references": [
                    {"name": "r_acct", "columns": ["acct"],
                     "target_table": "accounts",
                     "target_key": "k_acct", "rule": "mandatory"},
                    {"name": "r_branch", "columns": ["branch", "region"],
                     "target_table": "branches",
                     "target_key": "k_branch", "rule": "optional"}]},
            {
                "name": "terminated",
                "files": [str(paths["terminated"])],
                "row_id_path": ["TERM-ID"],
                "fields": {
                    "term_id": {"path": ["TERM-ID"], "type": "string"}},
                "candidate_keys": {"k_term": ["term_id"]},
                "references": []},
        ]
    }
    out = HERE / "run" / "contract.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(contract, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    paths = {
        "accounts": decode_real_accounts(),
        "branches": build_branches(),
        "terminated": build_terminated(),
    }
    txn_paths = build_transactions()
    paths["txns.part1"] = txn_paths[0]
    paths["txns.part2"] = txn_paths[1]
    contract = build_contract(paths)
    print("inputs written under", WORK)
    print("contract:", contract)


if __name__ == "__main__":
    main()
