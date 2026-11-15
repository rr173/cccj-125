#!/usr/bin/env python3
"""End-to-end runner for the settlement example scenarios.

For every scenario in expected_results.py this runner invokes the real
``copybook settle`` CLI as a subprocess, checks the documented exit code,
then compares the delivered result files against the hand-written
expectations.  It also exercises the ``copybook trace`` entry over a
delivered directory and proves that re-running into an existing
directory is refused.  The runner passes every path explicitly and never
imports the settlement engine to compute expected values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from expected_results import EXPECTED          # noqa: E402

CONFIG = HERE / "run" / "config" / "settle_config.json"
DATA = HERE / "run" / "data"
DELIVERY = HERE / "run" / "delivery"


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line]


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "copybook", *args],
        cwd=PROJECT, capture_output=True, text=True)


def _inputs_for(name: str):
    folder = DATA / name
    return sorted(str(p) for p in folder.glob("*.jsonl"))


def _settle(name: str, out_dir: Path):
    exp = EXPECTED[name]
    return _cli("settle", *_inputs_for(name), "--config", str(CONFIG),
                "--cutoff", exp["cutoff"], "--out-dir", str(out_dir))


def _active_edges(out_dir: Path):
    return sorted(
        (a["bill_id"], a["receipt_id"], a["amount"])
        for a in _read_jsonl(out_dir / "allocations.jsonl")
        if a["status"] == "active")


def check(name: str, failures: list):
    exp = EXPECTED[name]
    out_dir = DELIVERY / name
    proc = _settle(name, out_dir)
    if proc.returncode != exp["exit_code"]:
        failures.append(
            f"{name}: exit code {proc.returncode} != {exp['exit_code']} "
            f"(stderr: {proc.stderr.strip()})")
        return
    if exp["exit_code"] == 2:
        if out_dir.exists():
            failures.append(f"{name}: fatal run left a result directory")
        return

    report = json.loads((out_dir / "settle_report.json").read_text())
    if "allocations" in exp:
        got = sorted(
            (a["bill_id"], a["receipt_id"], a["amount"], a["status"],
             a["release_reason"])
            for a in _read_jsonl(out_dir / "allocations.jsonl"))
        if got != sorted(exp["allocations"]):
            failures.append(f"{name}: allocation history {got} != "
                            f"{sorted(exp['allocations'])}")
    if "active" in exp and _active_edges(out_dir) != sorted(exp["active"]):
        failures.append(f"{name}: active edges {_active_edges(out_dir)} "
                        f"!= {sorted(exp['active'])}")
    if "balances" in exp:
        got = {o["object_id"]: (o["kind"], o["remaining_amount"])
               for o in _read_jsonl(out_dir / "objects.jsonl")}
        if got != exp["balances"]:
            failures.append(f"{name}: balances {got} != {exp['balances']}")
    if "conservation" in exp:
        cons = report["groups"][0]["conservation"]
        want = exp["conservation"]
        mapping = {
            "bill_original": "bill_original_cents",
            "bill_open": "bill_open_cents",
            "receipt_original": "receipt_original_cents",
            "receipt_unallocated": "receipt_unallocated_cents",
            "active_allocation": "active_allocation_cents"}
        for label, key in mapping.items():
            if _cents_text(cons[key]) != want[label]:
                failures.append(
                    f"{name}: conservation {label} "
                    f"{_cents_text(cons[key])} != {want[label]}")
        if not (cons["bill_invariant_ok"] and cons["receipt_invariant_ok"]):
            failures.append(f"{name}: conservation invariants failed")
    if "checkpoints" in exp:
        for cutoff, want_edges in exp["checkpoints"].items():
            tag = cutoff.replace(":", "").replace("Z", "")
            cp_dir = DELIVERY / f"{name}-cp-{tag}"
            cp = _cli("settle", *_inputs_for(name), "--config",
                      str(CONFIG), "--cutoff", cutoff,
                      "--out-dir", str(cp_dir))
            if cp.returncode != 0:
                failures.append(f"{name}: checkpoint {cutoff} failed: "
                                f"{cp.stderr.strip()}")
                continue
            if _active_edges(cp_dir) != sorted(want_edges):
                failures.append(
                    f"{name}: checkpoint {cutoff} edges "
                    f"{_active_edges(cp_dir)} != {sorted(want_edges)}")
    if "at_utc" in exp:
        got = {e["event_id"]: e["at_utc"]
               for e in _read_jsonl(out_dir / "events.jsonl")}
        for eid, want in exp["at_utc"].items():
            if got.get(eid) != want:
                failures.append(f"{name}: {eid} at_utc {got.get(eid)} != "
                                f"{want}")
    if "applied_event_count" in exp:
        if report["applied_event_count"] != exp["applied_event_count"]:
            failures.append(f"{name}: applied count mismatch")
    if "bucket_amounts" in exp:
        got = report["groups"][0]["aging"]["bucket_amounts"]
        if got != exp["bucket_amounts"]:
            failures.append(f"{name}: aging buckets {got} != "
                            f"{exp['bucket_amounts']}")
        if report["groups"][0]["aging"]["base_day"] != exp["base_day"]:
            failures.append(f"{name}: aging base day mismatch")
    if "receipt_unallocated" in exp:
        cons = report["groups"][0]["conservation"]
        if _cents_text(cons["receipt_unallocated_cents"]) != \
                exp["receipt_unallocated"]:
            failures.append(f"{name}: unallocated mismatch")
    if "amount_text" in exp:
        edge = _read_jsonl(out_dir / "allocations.jsonl")[0]
        if edge["amount"] != exp["amount_text"] or \
                edge["amount_cents"] != exp["amount_cents"]:
            failures.append(f"{name}: huge amount not preserved exactly")
    if name == "bad_group":
        groups = {g["account"]: g for g in report["groups"]}
        good = groups[exp["good_group"]]
        bad = groups[exp["bad_group"]]
        if good["status"] != "valid" or bad["status"] != "invalid":
            failures.append(f"{name}: group statuses wrong")
        if [o for o in _read_jsonl(out_dir / "objects.jsonl")
                if o["account"] == exp["bad_group"]]:
            failures.append(f"{name}: invalid group delivered balances")
        codes = [e["code"] for e in bad["errors"]]
        if codes != exp["bad_error_codes"]:
            failures.append(f"{name}: error codes {codes} != "
                            f"{exp['bad_error_codes']}")
    if name == "hand_shuffled":
        base = _read_jsonl(DELIVERY / "hand" / "allocations.jsonl")
        got = _read_jsonl(out_dir / "allocations.jsonl")
        # Same edges, amounts and statuses regardless of file arrangement.
        comparable = [(a["bill_id"], a["receipt_id"], a["amount_cents"],
                       a["status"], a["release_reason"]) for a in got]
        reference = [(a["bill_id"], a["receipt_id"], a["amount_cents"],
                      a["status"], a["release_reason"]) for a in base]
        if sorted(comparable) != sorted(reference):
            failures.append(f"{name}: shuffled inputs changed destinations")


def _cents_text(cents: int) -> str:
    whole, frac = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}{whole}.{frac:02d}"


def check_trace(failures: list):
    """The trace entry must reconstruct Q's history from result files only."""
    out_dir = DELIVERY / "hand"
    proc = _cli("trace", str(out_dir), "Q", "--kind", "RECEIPT")
    if proc.returncode != 0:
        failures.append(f"trace: exit {proc.returncode}: {proc.stderr}")
        return
    payload = json.loads(proc.stdout)
    bills = sorted(a["bill_id"] for a in
                   payload["matches"][0]["allocation_history"])
    if bills != ["A", "B", "C"]:
        failures.append(f"trace: Q history bills {bills} != [A, B, C]")
    event_ids = sorted(e["event_id"]
                       for e in payload["matches"][0]["related_events"])
    if event_ids != ["e04", "e08", "e09"]:
        failures.append(f"trace: related events {event_ids}")


def check_no_overwrite(failures: list):
    out_dir = DELIVERY / "hand"
    proc = _settle("hand", out_dir)
    if proc.returncode != 2 or "already exists" not in proc.stderr:
        failures.append("re-run into an existing dir was not refused")


def main() -> int:
    if DELIVERY.exists():
        shutil.rmtree(DELIVERY)
    DELIVERY.mkdir(parents=True)
    failures: list = []
    for name in EXPECTED:
        check(name, failures)
    check_trace(failures)
    check_no_overwrite(failures)
    if failures:
        print("SETTLE SCENARIO FAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"all {len(EXPECTED)} scenarios + trace + no-overwrite checks "
          "match the hand-written expectations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
