#!/usr/bin/env python3
"""Streaming-scale driver: one giant batch and many small batches.

Generates decoded evidence directly (the same JSONL shapes the real
``write_jsonl`` / ``export_quarantine`` produce), then runs the auditor in
process while measuring wall time, peak RSS and SQLite staging size.  The
point is to show that peak memory stays flat as detail counts grow: every
detail payload is staged on disk and only the current line plus the fixed
category table live in memory.

Run:  python3 examples/audit/run_performance.py [out_dir]
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.audit import AuditConfig, run_audit  # noqa: E402

SOURCE = "/landing/perf_feed.dat"
REC_WIDTH = 74           # 73 record bytes + newline


def _record(line_no: int, fields: dict) -> dict:
    start = (line_no - 1) * REC_WIDTH
    return {
        "meta": {"source_file": SOURCE, "line_number": line_no,
                 "byte_start": start, "byte_end": start + 73,
                 "layout": "HANDOFF", "layout_version": "1"},
        "fields": fields,
    }


def _config() -> AuditConfig:
    return AuditConfig.load(HERE / "audit_config.json")


def _rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _format_milli(milli: int) -> str:
    if milli >= 0:
        whole, frac = divmod(milli, 1000)
        return f"{whole}.{frac:03d}"
    whole, frac = divmod(-milli, 1000)
    return f"-{whole}.{frac:03d}"


def build_giant_batch(root: Path, details: int) -> tuple[Path, Path, int]:
    """One BHEAD..BTAIL batch containing *details* accepted detail lines.

    Evidence is written incrementally; the generator itself never holds the
    whole file in memory.
    """
    work = root / "giant" / "work"
    work.mkdir(parents=True)
    records_path = work / "records.jsonl"
    quarantine_path = work / "quarantine.jsonl"
    quarantine_path.write_text("", encoding="utf-8")

    line = 0

    def emit(fh, fields):
        nonlocal line
        line += 1
        fh.write(json.dumps(_record(line, fields)) + "\n")

    aaa_sum = 0
    with records_path.open("w", encoding="utf-8") as fh:
        emit(fh, {"REC-TYPE": "FHEAD", "FILE-ID": "F-GIANT"})
        emit(fh, {"REC-TYPE": "BHEAD", "BATCH-ID": "GIANT-1"})
        for seq in range(1, details + 1):
            milli = (seq % 7) - 3            # cycles -3..+3, exact integers
            aaa_sum += milli
            emit(fh, {"REC-TYPE": "DETAIL", "BATCH-ID": "GIANT-1",
                      "SEQ-NUM": str(seq), "CATEGORY": "AAA",
                      "QUANTITY": _format_milli(milli)})
        emit(fh, {"REC-TYPE": "BTAIL", "BATCH-ID": "GIANT-1",
                  "DETAIL-COUNT": str(details),
                  "TOTAL-AAA": _format_milli(aaa_sum),
                  "TOTAL-BBB": "0.000"})
        emit(fh, {"REC-TYPE": "FTAIL", "FILE-ID": "F-GIANT",
                  "BATCH-COUNT": "1", "DETAIL-COUNT": str(details)})
    return records_path, quarantine_path, line


def build_many_batches(root: Path, batches: int,
                       per_batch: int) -> tuple[Path, Path, int]:
    """Many small batches, each with *per_batch* details; seq restarts."""
    work = root / "many" / "work"
    work.mkdir(parents=True)
    records_path = work / "records.jsonl"
    quarantine_path = work / "quarantine.jsonl"
    quarantine_path.write_text("", encoding="utf-8")

    line = 0

    def emit(fh, fields):
        nonlocal line
        line += 1
        fh.write(json.dumps(_record(line, fields)) + "\n")

    total_positions = batches * per_batch
    with records_path.open("w", encoding="utf-8") as fh:
        emit(fh, {"REC-TYPE": "FHEAD", "FILE-ID": "F-MANY"})
        for b in range(1, batches + 1):
            batch_id = f"MB-{b:06d}"
            emit(fh, {"REC-TYPE": "BHEAD", "BATCH-ID": batch_id})
            aaa_sum = 0
            for seq in range(1, per_batch + 1):
                milli = 1000 + seq
                aaa_sum += milli
                emit(fh, {"REC-TYPE": "DETAIL", "BATCH-ID": batch_id,
                          "SEQ-NUM": str(seq), "CATEGORY": "AAA",
                          "QUANTITY": _format_milli(milli)})
            emit(fh, {"REC-TYPE": "BTAIL", "BATCH-ID": batch_id,
                      "DETAIL-COUNT": str(per_batch),
                      "TOTAL-AAA": _format_milli(aaa_sum),
                      "TOTAL-BBB": "0.000"})
        emit(fh, {"REC-TYPE": "FTAIL", "FILE-ID": "F-MANY",
                  "BATCH-COUNT": str(batches),
                  "DETAIL-COUNT": str(total_positions)})
    return records_path, quarantine_path, line


def _measure(label: str, records_path: Path, quarantine_path: Path,
             expected_lines: int, root: Path, name: str) -> dict:
    out_dir = root / name / "delivery"
    temp_dir = root / name / "staging"
    rss_before = _rss_kb()
    t0 = time.perf_counter()
    summary = run_audit(
        records_path, quarantine_path, config=_config(),
        expected_source=SOURCE, expected_lines=expected_lines,
        output_dir=out_dir, temp_dir=temp_dir)
    elapsed = time.perf_counter() - t0
    rss_peak = _rss_kb()
    report = json.loads((out_dir / "audit_report.json").read_text())
    delivered = sum(1 for _ in (out_dir / "accepted_details.jsonl").open())
    return {
        "label": label,
        "expected_lines": expected_lines,
        "conclusion": summary["conclusion"],
        "batch_count": summary["batch_count"],
        "rejected_batch_count": summary["rejected_batch_count"],
        "accepted_detail_count": summary["accepted_detail_count"],
        "delivered_jsonl_rows": delivered,
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_kb_before": rss_before,
        "peak_rss_kb": rss_peak,
        "rss_growth_kb": rss_peak - rss_before,
        "staging_bytes_at_completion": summary["staging_bytes"],
        "report_bytes": (out_dir / "audit_report.json").stat().st_size,
        "accepted_file_bytes": (out_dir /
                                "accepted_details.jsonl").stat().st_size,
    }


def main() -> int:
    root = HERE / "perf_run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    giant_details = int(os.environ.get("GIANT_DETAILS", "200000"))
    many_batches = int(os.environ.get("MANY_BATCHES", "5000"))
    many_per_batch = int(os.environ.get("MANY_PER_BATCH", "4"))

    g_recs, g_quar, g_lines = build_giant_batch(root, giant_details)
    m_recs, m_quar, m_lines = build_many_batches(
        root, many_batches, many_per_batch)

    results = [
        _measure(f"single giant batch ({giant_details} details)",
                 g_recs, g_quar, g_lines, root, "giant"),
        _measure(f"{many_batches} small batches x {many_per_batch} details",
                 m_recs, m_quar, m_lines, root, "many"),
    ]

    print(json.dumps(results, indent=2))
    (root / "performance_summary.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {root / 'performance_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
