#!/usr/bin/env python3
"""End-to-end scenario runner for the masking sample generator.

For each scenario this script:

1. materialises the hand-constructed inputs (``build_data.py``);
2. invokes the real ``copybook mask`` CLI as a subprocess so the
   documented exit codes are exercised;
3. compares the delivered dataset and the reports against the
   hand-written expectations in ``expected_results.py`` field by field;
4. re-runs the main scenario with shuffled input order and a different
   chunk size and requires byte-identical output.

The runner lives outside the generated data directory and passes all
paths as arguments; it never imports the untrusted data as code.
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

from build_data import build_all                      # noqa: E402
from expected_results import (EXPECTED_FAILURES, EXPECTED_MAIN,  # noqa: E402
                              EXIT_FAILED, EXIT_OK)

RUN = HERE / "run"
POLICY = HERE / "mask_policy.json"
KEY_FILE = HERE / "demo_key.txt"

MAIN_INPUTS = ["accounts.jsonl", "txns.jsonl", "related.jsonl"]


def _mask(inputs, policy, out_dir, key_file=KEY_FILE, chunk_size=None):
    cmd = [
        sys.executable, "-m", "copybook", "mask",
        *[str(p) for p in inputs],
        "--policy", str(policy),
        "--key-file", str(key_file),
        "--out-dir", str(out_dir),
    ]
    if chunk_size is not None:
        cmd += ["--chunk-size", str(chunk_size)]
    return subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)


def _load_jsonl(path: Path) -> list:
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()]


def _check(failures: list, condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def check_main(root: Path) -> list[str]:
    exp = EXPECTED_MAIN
    failures: list[str] = []
    out_dir = root / "main" / "delivery"
    inputs = [root / "data" / name for name in MAIN_INPUTS]
    proc = _mask(inputs, POLICY, out_dir)
    _check(failures, proc.returncode == exp["exit_code"],
           f"main: exit {proc.returncode}, expected {exp['exit_code']}: "
           f"{proc.stderr.strip()}")
    if proc.returncode != 0:
        return failures

    # ---- file inventory
    listing = sorted(p.name for p in out_dir.iterdir())
    _check(failures, listing == exp["output_files"],
           f"main: output files {listing} != {exp['output_files']}")

    # ---- per-file masked records, field by field
    actual = {}
    for stem in ("accounts", "txns", "related"):
        actual[stem] = _load_jsonl(out_dir / f"{stem}.masked.jsonl")
    for stem, expected_rows in (("accounts", exp["accounts"]),
                                ("txns", exp["txns"]),
                                ("related", exp["related"])):
        rows = actual[stem]
        _check(failures, len(rows) == len(expected_rows),
               f"main: {stem} has {len(rows)} rows, "
               f"expected {len(expected_rows)}")
        for got, want in zip(rows, expected_rows):
            _check(failures,
                   got["meta"]["line_number"] == want["line"],
                   f"main: {stem} line {want['line']}: meta line_number "
                   f"is {got['meta']['line_number']}")
            _check(failures, got["fields"] == want["fields"],
                   f"main: {stem} line {want['line']}: fields differ:\n"
                   f"  got  {got['fields']}\n"
                   f"  want {want['fields']}")

    # ---- structural relations, independent of any derived value
    acct_by_line = {r["meta"]["line_number"]: r["fields"]
                    for r in actual["accounts"]}
    txn_accts = [r["fields"]["ACCT-NO"] for r in actual["txns"]]
    rel_accts = [r["fields"]["ACCT-NO"] for r in actual["related"]]
    # same subject joins across account / detail / related tables
    _check(failures,
           acct_by_line[1]["ACCT-NO"] == txn_accts[0] == txn_accts[1]
           == rel_accts[0],
           "main: subject 1000002345 does not join across files")
    _check(failures, acct_by_line[2]["ACCT-NO"] == txn_accts[2]
           == rel_accts[1],
           "main: subject 1000002346 does not join across files")
    # ALT-ACCT carries the same fake as ACCT-NO inside one record
    _check(failures,
           acct_by_line[1]["ACCT-NO"] == acct_by_line[1]["ALT-ACCT"],
           "main: ACCT-NO and ALT-ACCT of line 1 diverge")
    # distinct originals never merge
    fakes = {r["fields"]["ACCT-NO"]
             for r in actual["accounts"] + actual["txns"]
             + actual["related"]}
    _check(failures, len(fakes) == 4,
           f"main: expected 4 distinct delivered fakes, got {len(fakes)}")
    for fake in fakes:
        shape = exp["fake_shape"]
        _check(failures,
               len(fake) == shape["length"] and fake.isdigit(),
               f"main: fake {fake!r} lost the 10-digit account shape")
    # null vs missing are different: line 3 keeps null, line 4 lacks keys
    _check(failures, acct_by_line[3]["CUST-NAME"] is None,
           "main: null CUST-NAME was not kept as null")
    _check(failures, "CUST-NAME" not in acct_by_line[4]
           and "ALT-ACCT" not in acct_by_line[4],
           "main: missing fields reappeared in line 4")
    # unlisted fields pass through untouched
    _check(failures, acct_by_line[1]["BRANCH-CODE"] == "BR01",
           "main: BRANCH-CODE was not preserved")
    # same original date shifts identically in every file of the group
    _check(failures,
           acct_by_line[1]["OPEN-DATE"] == actual["txns"][0]
           ["fields"]["TXN-DATE"],
           "main: shared bizdate group did not shift 2024-01-15 "
           "identically")

    # ---- no original sensitive value survives anywhere in the delivery
    blob = "".join(p.read_text(encoding="utf-8")
                   for p in out_dir.iterdir() if p.is_file())
    for fragment in exp["forbidden_fragments"]:
        _check(failures, fragment not in blob,
               f"main: original fragment {fragment!r} leaked into output")
    _check(failures, "DEMO-ONLY-mask-key" not in blob,
           "main: the mapping key leaked into the delivery")

    # ---- report contents
    report = json.loads((out_dir / "mask_report.json")
                        .read_text(encoding="utf-8"))
    _check(failures, report["conclusion"] == "masked",
           "main: report conclusion is not 'masked'")
    for key, want in exp["summary"].items():
        got = report["totals"].get(key, None)
        if key in ("file_count", "rule_count", "group_count",
                   "distinct_pseudonym_values"):
            got = {"file_count": len(report["files"]),
                   "rule_count": len(report["policy"]["rules"]),
                   "group_count": len(report["policy"]["groups"]),
                   "distinct_pseudonym_values":
                       report["policy"]["groups"][0]["distinct_values"]
                       }[key]
        _check(failures, got == want,
               f"main: summary {key} is {got}, expected {want}")
    for f in report["files"]:
        stem = Path(f["input"]).stem
        want = exp["file_counts"][stem]
        for key, value in want.items():
            _check(failures, f[key] == value,
                   f"main: {stem} {key} is {f[key]}, expected {value}")
    _check(failures, len(report["rejections"]) == 1,
           "main: expected exactly one rejection")
    if report["rejections"]:
        rej = report["rejections"][0]
        want = exp["rejections"][0]
        _check(failures, rej["line_number"] == want["line_number"]
               and rej["path"] == want["path"]
               and rej["reason"] == want["reason"],
               f"main: rejection mismatch: {rej}")
    for rule in report["policy"]["rules"]:
        want = exp["rule_stats"][rule["path_text"]]
        for key, value in want.items():
            _check(failures, rule[key] == value,
                   f"main: rule {rule['path_text']} stat {key} is "
                   f"{rule[key]}, expected {value}")
    _check(failures,
           report["key_fingerprint"] == "sha256:aba28c86450ca4e6",
           "main: wrong key fingerprint in report")
    _check(failures, len(report["notes"]) >= 3,
           "main: boundary notes missing from report")

    # ---- per-record report: one line per input record
    record_report = _load_jsonl(out_dir / "record_report.jsonl")
    _check(failures, len(record_report) == exp["summary"]["records_in"],
           f"main: record_report has {len(record_report)} lines, "
           f"expected {exp['summary']['records_in']}")
    rejected = [r for r in record_report if r["status"] == "rejected"]
    _check(failures, len(rejected) == 1
           and rejected[0]["line_number"] == 5
           and rejected[0]["rejected_by"] == ["ALT-ACCT"],
           f"main: record_report rejection mismatch: {rejected}")
    return failures


def check_order_chunk_invariance(root: Path) -> list[str]:
    failures: list[str] = []
    main_dir = root / "main" / "delivery"
    shuffled_dir = root / "shuffled" / "delivery"
    # reversed input order, chunk size 1 instead of the default
    inputs = [root / "data" / name for name in reversed(MAIN_INPUTS)]
    proc = _mask(inputs, POLICY, shuffled_dir, chunk_size=1)
    _check(failures, proc.returncode == EXIT_OK,
           f"invariance: exit {proc.returncode}: {proc.stderr.strip()}")
    if proc.returncode != 0:
        return failures
    main_files = sorted(p.name for p in main_dir.iterdir())
    shuffled_files = sorted(p.name for p in shuffled_dir.iterdir())
    _check(failures, main_files == shuffled_files,
           "invariance: file inventory changed with input order")
    for name in main_files:
        before = (main_dir / name).read_bytes()
        after = (shuffled_dir / name).read_bytes()
        _check(failures, before == after,
               f"invariance: {name} differs between runs")
    return failures


def _write_policy(root: Path, name: str, extra_rule: dict) -> Path:
    base = json.loads(POLICY.read_text(encoding="utf-8"))
    base["fields"].append(extra_rule)
    path = root / name
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return path


def check_failures(root: Path) -> list[str]:
    failures: list[str] = []
    data = root / "data"
    inputs = [data / name for name in MAIN_INPUTS]

    bad_path_policy = _write_policy(
        root, "policy_bad_path.json",
        {"path": ["NO-SUCH-FIELD"], "action": "synthesize",
         "kind": "name"})
    bad_type_policy = _write_policy(
        root, "policy_bad_type.json",
        {"path": ["BRANCH-CODE"], "action": "shift", "kind": "amount",
         "max_pct": "5"})
    wrong_key = root / "wrong_key.txt"
    wrong_key.write_text("some-other-key-material-987654321",
                         encoding="utf-8")

    scenarios = {
        "policy_path_never_matched": dict(
            inputs=inputs, policy=bad_path_policy),
        "policy_type_mismatch": dict(
            inputs=inputs, policy=bad_type_policy),
        "wrong_key": dict(inputs=inputs, policy=POLICY,
                          key_file=wrong_key),
        "corrupt_input": dict(
            inputs=[root / "corrupt" / name for name in MAIN_INPUTS],
            policy=POLICY),
    }
    for name, kwargs in scenarios.items():
        out_dir = root / name / "delivery"
        proc = _mask(kwargs["inputs"], kwargs["policy"], out_dir,
                     key_file=kwargs.get("key_file", KEY_FILE))
        want = EXPECTED_FAILURES[name]
        _check(failures, proc.returncode == want["exit_code"],
               f"{name}: exit {proc.returncode}, expected "
               f"{want['exit_code']}")
        _check(failures, want["stderr_contains"] in proc.stderr,
               f"{name}: stderr lacks {want['stderr_contains']!r}: "
               f"{proc.stderr.strip()}")
        _check(failures, not out_dir.exists(),
               f"{name}: output directory was left behind")

    # existing output directory: pre-create it, then attempt the run
    out_dir = root / "existing_output" / "delivery"
    out_dir.mkdir(parents=True)
    proc = _mask(inputs, POLICY, out_dir)
    want = EXPECTED_FAILURES["existing_output"]
    _check(failures, proc.returncode == want["exit_code"],
           f"existing_output: exit {proc.returncode}")
    _check(failures, want["stderr_contains"] in proc.stderr,
           f"existing_output: stderr lacks {want['stderr_contains']!r}")
    _check(failures, out_dir.exists() and not any(out_dir.iterdir()),
           "existing_output: the pre-existing directory was modified")
    return failures


def main() -> int:
    if RUN.exists():
        shutil.rmtree(RUN)
    RUN.mkdir(parents=True)
    build_all(RUN)

    all_failures: list[str] = []
    for label, fn in (("main", check_main),
                      ("order/chunk invariance",
                       check_order_chunk_invariance),
                      ("failure scenarios", check_failures)):
        failures = fn(RUN)
        status = "OK" if not failures else f"{len(failures)} FAILURE(S)"
        print(f"[{label}] {status}")
        all_failures.extend(failures)

    if all_failures:
        print("\nFAILURES:")
        for failure in all_failures:
            print(f"  - {failure}")
        return 1
    print("\nall mask scenarios match the hand-written expectations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
