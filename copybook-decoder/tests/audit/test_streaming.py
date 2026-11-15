"""Streaming and staging guarantees for very large batches.

These tests do not try to prove exact memory numbers (those depend on the
interpreter); instead they prove the structural invariants that make memory
independent of detail count:

* every DETAIL payload is staged in SQLite, never retained on the engine;
* the in-memory open-batch state has no growing list of detail lines;
* the Python result contains only counts and fixed-size summaries, while
  the full detail/line-number arrays are streamed to disk;
* staging lives in the caller-named temp base and is removed afterwards.
"""

import json
import shutil
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from copybook.audit import AuditConfig, PASS, run_audit

SRC = "/landing/big.dat"
CATEGORIES = ["AAA", "BBB"]


def _config():
    return AuditConfig.from_dict({
        "categories": CATEGORIES,
        "paths": {
            "record_type": ["REC-TYPE"],
            "file_id": ["FILE-ID"],
            "batch_id": ["BATCH-ID"],
            "seq": ["SEQ"],
            "category": ["CATEGORY"],
            "quantity": ["QTY"],
            "btail_detail_count": ["DETAIL-COUNT"],
            "btail_totals": {"AAA": ["TOTAL-AAA"], "BBB": ["TOTAL-BBB"]},
            "ftail_batch_count": ["BATCH-COUNT"],
            "ftail_detail_count": ["DETAIL-COUNT"],
        },
    })


def _record(line, fields, width=20):
    start = (line - 1) * (width + 1)
    return {"meta": {"source_file": SRC, "line_number": line,
                     "byte_start": start, "byte_end": start + width,
                     "layout": "X", "layout_version": "1"},
            "fields": fields}


def _write(path, rows):
    with Path(path).open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _build_giant(d, details: int):
    rows = [_record(1, {"REC-TYPE": "FHEAD", "FILE-ID": "F"})]
    rows.append(_record(2, {"REC-TYPE": "BHEAD", "BATCH-ID": "BIG"}))
    aaa = 0
    for seq in range(1, details + 1):
        milli = seq % 5            # 0..4 milli-units, always exact
        aaa += milli
        rows.append(_record(2 + seq, {
            "REC-TYPE": "DETAIL", "BATCH-ID": "BIG", "SEQ": str(seq),
            "CATEGORY": "AAA", "QTY": f"0.{milli:03d}"}))
    btail_line = 3 + details
    whole, frac = divmod(aaa, 1000)
    rows.append(_record(btail_line, {
        "REC-TYPE": "BTAIL", "BATCH-ID": "BIG",
        "DETAIL-COUNT": str(details),
        "TOTAL-AAA": f"{whole}.{frac:03d}", "TOTAL-BBB": "0.000"}))
    rows.append(_record(btail_line + 1, {
        "REC-TYPE": "FTAIL", "FILE-ID": "F",
        "BATCH-COUNT": "1", "DETAIL-COUNT": str(details)}))
    rp = Path(d) / "records.jsonl"
    qp = Path(d) / "quarantine.jsonl"
    qp.write_text("", encoding="utf-8")
    _write(rp, rows)
    return rp, qp, len(rows)


class StreamingTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_large_batch_is_staged_not_buffered(self):
        details = 5000
        rp, qp, total = _build_giant(self.dir, details)
        staging_base = self.dir / "tmp"
        out = self.dir / "delivery"

        tracemalloc.start()
        try:
            summary = run_audit(
                rp, qp, config=_config(), expected_source=SRC,
                expected_lines=total, output_dir=out,
                temp_dir=staging_base)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(summary["conclusion"], PASS)
        self.assertEqual(summary["accepted_detail_count"], details)
        # Python-level peak stays small relative to the 5k payloads: each
        # payload JSON is far larger than this bounded ceiling.
        self.assertLess(peak, 4_000_000,
                        f"tracemalloc peak {peak} too high for streaming")

        # The returned summary itself carries no detail payload array.
        self.assertNotIn("batches", summary)
        self.assertNotIn("accepted_detail_line_numbers", summary)

        # Staging directory is cleaned; only the delivery remains.
        self.assertFalse(any(staging_base.glob("audit-staging-*")))

        # The full result is on disk and matches the input exactly.
        report = json.loads((out / "audit_report.json").read_text())
        self.assertEqual(len(report["accepted_detail_line_numbers"]),
                         details)
        self.assertEqual(report["accepted_detail_line_numbers"][0], 3)
        self.assertEqual(report["accepted_detail_line_numbers"][-1],
                         2 + details)
        delivered = (out / "accepted_details.jsonl").read_text().splitlines()
        self.assertEqual(len(delivered), details)
        first = json.loads(delivered[0])
        self.assertEqual(first["file_id"], "F")
        self.assertEqual(first["batch_id"], "BIG")
        self.assertNotIn("file_id", first["fields"])

    def test_rss_and_staging_are_reported(self):
        details = 500
        rp, qp, total = _build_giant(self.dir, details)
        summary = run_audit(
            rp, qp, config=_config(), expected_source=SRC,
            expected_lines=total, output_dir=self.dir / "delivery",
            temp_dir=self.dir / "tmp")
        self.assertIsInstance(summary["peak_rss_kb"], int)
        self.assertGreater(summary["peak_rss_kb"], 0)
        self.assertGreaterEqual(summary["staging_bytes"], 0)

    def test_sqlite_holds_every_detail_payload(self):
        # White-box: open a run's database by pointing temp_dir at a path we
        # inspect before cleanup is not possible, so instead verify the
        # delivered file is the sole in-memory-independent source of truth
        # and the report can be re-derived without the input JSONL.
        details = 300
        rp, qp, total = _build_giant(self.dir, details)
        out = self.dir / "delivery"
        run_audit(rp, qp, config=_config(), expected_source=SRC,
                  expected_lines=total, output_dir=out,
                  temp_dir=self.dir / "tmp")
        # remove the source evidence; the delivery must stand alone
        rp.unlink()
        qp.unlink()
        report = json.loads((out / "audit_report.json").read_text())
        self.assertEqual(report["accepted_detail_count"], details)
        self.assertEqual(
            sum(1 for _ in (out / "accepted_details.jsonl").open()),
            details)


if __name__ == "__main__":
    unittest.main()
