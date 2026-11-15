#!/usr/bin/env python3
"""End-to-end scenario runner.

For every scenario in expected_results.py this script:

1. decodes the generated EBCDIC .dat with the real ``copybook decode``
   pipeline (write_jsonl + export_quarantine artifacts);
2. invokes the real ``copybook audit`` CLI as a subprocess so the documented
   exit codes are exercised;
3. compares the audit report and delivered details against the hand-written
   expectations line by line.

The runner lives outside the generated data directory and passes all paths
as arguments; it never imports the untrusted data as code.
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

from build_scenarios import RECORD_LENGTH, build_all  # noqa: E402
from expected_results import EXPECTED                # noqa: E402

SOURCE_NAME = "feed.dat"


def _decode(dat_path: Path, work: Path) -> tuple[Path, Path, str]:
    records = work / "records.jsonl"
    qdir = work / "quarantine"
    cmd = [
        sys.executable, "-m", "copybook", "decode", str(dat_path),
        "--manifest", str(HERE / "manifest.json"),
        "--sender", "BANKX", "--date", "2024-01-15",
        "--out", str(records),
        "--quarantine-dir", str(qdir),
    ]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"decode failed for {dat_path.name}: {proc.stderr}")
    index = qdir / f"{dat_path.stem}.quarantine.jsonl"
    return records, index, str(dat_path)


def _audit(records: Path, index: Path, source: str, expected_lines: int,
           out_dir: Path, temp_dir: Path) -> tuple[int, str, str]:
    cmd = [
        sys.executable, "-m", "copybook", "audit",
        str(records), str(index),
        "--config", str(HERE / "audit_config.json"),
        "--expected-source", source,
        "--expected-lines", str(expected_lines),
        "--out-dir", str(out_dir),
        "--temp-dir", str(temp_dir),
    ]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _drop_evidence_line(records: Path, quarantine: Path,
                        drop_line: int) -> None:
    """Remove one physical line's evidence to simulate an incomplete set."""
    for path in (records, quarantine):
        kept = []
        for line in path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            number = obj.get("line_number") or obj.get("meta",
                                                       {}).get("line_number")
            if number != drop_line:
                kept.append(line)
        path.write_text("\n".join(kept) + ("\n" if kept else ""),
                        encoding="utf-8")


def check_scenario(name: str, root: Path) -> list[str]:
    exp = EXPECTED[name]
    work = root / name / "work"
    delivery = root / name / "delivery"
    staging = root / name / "staging"
    work.mkdir(parents=True)
    staging.mkdir(parents=True)

    dat = root / "data" / f"{name}.dat"
    if name == "line_gap":
        # Reuse the good handoff file, then punch a hole in its evidence.
        dat = root / "data" / "handoff_good.dat"
    records, index, source = _decode(dat, work)

    if name == "line_gap":
        _drop_evidence_line(records, index, drop_line=4)

    code, stdout, stderr = _audit(
        records, index, source, exp["physical_lines"], delivery, staging)
    errors: list[str] = []

    if code != exp["exit_code"]:
        errors.append(f"exit code {code} != expected {exp['exit_code']} "
                      f"(stderr={stderr.strip()})")

    if exp.get("input_error"):
        if delivery.exists():
            errors.append("unverifiable run must not create a delivery "
                          "directory")
        # staging subdirectory must still be cleaned on failure
        leftovers = [p for p in staging.glob("audit-staging-*")]
        if leftovers:
            errors.append(f"staging not cleaned after failure: {leftovers}")
        return errors

    report_path = delivery / "audit_report.json"
    if not report_path.exists():
        errors.append("audit_report.json missing")
        return errors
    report = json.loads(report_path.read_text(encoding="utf-8"))

    if report["conclusion"] != exp["conclusion"]:
        errors.append(f"conclusion {report['conclusion']!r} != "
                      f"{exp['conclusion']!r}")
    if report["file_rejected"] != exp["file_rejected"]:
        errors.append(f"file_rejected {report['file_rejected']} != "
                      f"{exp['file_rejected']}")
    if report["accepted_detail_line_numbers"] != exp["accepted_lines"]:
        errors.append("accepted lines "
                      f"{report['accepted_detail_line_numbers']} != "
                      f"{exp['accepted_lines']}")

    # quarantine line numbers actually produced by the decoder
    q_rows = [json.loads(l) for l in index.read_text().splitlines()] if (
        index.exists()) else []
    q_lines = sorted(q["line_number"] for q in q_rows)
    if q_lines != exp["quarantine_lines"]:
        errors.append(f"quarantine lines {q_lines} != "
                      f"{exp['quarantine_lines']}")

    # batch-level checks
    batches = report["batches"]
    if len(batches) != len(exp["batches"]):
        errors.append(f"batch count {len(batches)} != "
                      f"{len(exp['batches'])}")
    for got, want in zip(batches, exp["batches"]):
        prefix = f"batch#{want['ordinal']}"
        for key in ("batch_id", "status", "bhead_line", "btail_line"):
            if got.get(key) != want[key]:
                errors.append(f"{prefix} {key}: {got.get(key)!r} != "
                              f"{want[key]!r}")
        if got["detail_count"]["declared"] != \
                want["declared_detail_count"]:
            errors.append(f"{prefix} declared_detail_count "
                          f"{got['detail_count']['declared']!r} != "
                          f"{want['declared_detail_count']!r}")
        if got["detail_count"]["actual"] != want["actual_detail_count"]:
            errors.append(f"{prefix} actual_detail_count "
                          f"{got['detail_count']['actual']!r} != "
                          f"{want['actual_detail_count']!r}")
        if got["detail_count"]["ok"] != want["detail_count_ok"]:
            errors.append(f"{prefix} detail_count.ok mismatch")
        if got["quantity_check"]["status"] != want["quantity_status"]:
            errors.append(f"{prefix} quantity_status "
                          f"{got['quantity_check']['status']!r} != "
                          f"{want['quantity_status']!r}")
        got_codes = sorted(d["code"] for d in got["diagnostics"])
        if got_codes != sorted(want["diagnostic_codes"]):
            errors.append(f"{prefix} diagnostics {got_codes} != "
                          f"{sorted(want['diagnostic_codes'])}")
        if "totals_milli" in want:
            totals = {t["category"]: t["actual_milli"]
                      for t in got["category_totals"]}
            if any(undec for undec in
                   (t["undecidable"] for t in got["category_totals"])):
                errors.append(f"{prefix} totals unexpectedly undecidable")
            elif totals != want["totals_milli"]:
                errors.append(f"{prefix} totals {totals} != "
                              f"{want['totals_milli']}")

    # FTAIL control counts
    if "ftail" in exp:
        want_f = exp["ftail"]
        got_f = report["file_checks"]
        if got_f["ftail_line"] != want_f["line"]:
            errors.append("ftail line mismatch")
        if got_f["batch_count"]["declared"] != \
                want_f["batch_count_declared"] or \
                got_f["batch_count"]["actual"] != \
                want_f["batch_count_actual"]:
            errors.append(f"FTAIL batch_count {got_f['batch_count']} != "
                          f"{want_f}")
        if got_f["detail_count"]["declared"] != \
                want_f["detail_count_declared"] or \
                got_f["detail_count"]["actual"] != \
                want_f["detail_count_actual"]:
            errors.append(f"FTAIL detail_count {got_f['detail_count']} != "
                          f"{want_f}")

    if "file_diagnostic_codes" in exp:
        got_codes = sorted(d["code"] for d in report["file_diagnostics"])
        if got_codes != sorted(exp["file_diagnostic_codes"]):
            errors.append(f"file diagnostics {got_codes} != "
                          f"{sorted(exp['file_diagnostic_codes'])}")

    # delivered detail file content
    delivered_path = delivery / "accepted_details.jsonl"
    delivered = [json.loads(l)
                 for l in delivered_path.read_text().splitlines()] if (
        delivered_path.exists()) else []
    delivered_lines = [d["meta"]["line_number"] for d in delivered]
    if delivered_lines != exp["accepted_lines"]:
        errors.append(f"delivered detail lines {delivered_lines} != "
                      f"{exp['accepted_lines']}")
    for d in delivered:
        if "file_id" not in d or "batch_id" not in d:
            errors.append("delivered detail missing file_id/batch_id")
        if "file_id" in d.get("fields", {}) or "batch_id" in d.get(
                "fields", {}):
            errors.append("annotation must not overwrite original fields")

    return errors


def main() -> int:
    root = HERE / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    build_all(root / "data")

    all_ok = True
    for name in EXPECTED:
        errors = check_scenario(name, root)
        if errors:
            all_ok = False
            print(f"FAIL  {name}")
            for e in errors:
                print(f"        - {e}")
        else:
            print(f"PASS  {name}")
    print()
    print("scenarios:", "ALL OK" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
