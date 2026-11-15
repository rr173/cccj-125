"""CLI integration tests for the ``copybook audit`` exit-code contract."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]

SRC = "/landing/feed.dat"
CONFIG = {
    "categories": ["AAA"],
    "paths": {
        "record_type": ["REC-TYPE"],
        "file_id": ["FILE-ID"],
        "batch_id": ["BATCH-ID"],
        "seq": ["SEQ"],
        "category": ["CATEGORY"],
        "quantity": ["QTY"],
        "btail_detail_count": ["DETAIL-COUNT"],
        "btail_totals": {"AAA": ["TOTAL-AAA"]},
        "ftail_batch_count": ["BATCH-COUNT"],
        "ftail_detail_count": ["DETAIL-COUNT"],
    },
}


def _rec(line, fields, width=20):
    start = (line - 1) * (width + 1)
    return {"meta": {"source_file": SRC, "line_number": line,
                     "byte_start": start, "byte_end": start + width,
                     "layout": "X", "layout_version": "1"},
            "fields": fields}


class CliAuditTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.records = self.dir / "records.jsonl"
        self.quarantine = self.dir / "quarantine.jsonl"
        self.quarantine.write_text("", encoding="utf-8")
        self.config_path = self.dir / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.out = self.dir / "delivery"
        self.tmp = self.dir / "staging"

    def _run(self, expected_lines):
        return subprocess.run(
            [sys.executable, "-m", "copybook", "audit",
             str(self.records), str(self.quarantine),
             "--config", str(self.config_path),
             "--expected-source", SRC,
             "--expected-lines", str(expected_lines),
             "--out-dir", str(self.out),
             "--temp-dir", str(self.tmp)],
            cwd=PROJECT, capture_output=True, text=True)

    def _write(self, rows):
        self.records.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_exit_zero_when_everything_passes(self):
        self._write([
            _rec(1, {"REC-TYPE": "FHEAD", "FILE-ID": "F"}),
            _rec(2, {"REC-TYPE": "BHEAD", "BATCH-ID": "B1"}),
            _rec(3, {"REC-TYPE": "DETAIL", "BATCH-ID": "B1", "SEQ": "1",
                     "CATEGORY": "AAA", "QTY": "1.000"}),
            _rec(4, {"REC-TYPE": "BTAIL", "BATCH-ID": "B1",
                     "DETAIL-COUNT": "1", "TOTAL-AAA": "1.000"}),
            _rec(5, {"REC-TYPE": "FTAIL", "FILE-ID": "F",
                     "BATCH-COUNT": "1", "DETAIL-COUNT": "1"}),
        ])
        proc = self._run(5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.out / "audit_report.json").exists())

    def test_exit_one_on_business_rejection(self):
        self._write([
            _rec(1, {"REC-TYPE": "FHEAD", "FILE-ID": "F"}),
            _rec(2, {"REC-TYPE": "BHEAD", "BATCH-ID": "B1"}),
            _rec(3, {"REC-TYPE": "DETAIL", "BATCH-ID": "B1", "SEQ": "9",
                     "CATEGORY": "AAA", "QTY": "1.000"}),
            _rec(4, {"REC-TYPE": "BTAIL", "BATCH-ID": "B1",
                     "DETAIL-COUNT": "1", "TOTAL-AAA": "1.000"}),
            _rec(5, {"REC-TYPE": "FTAIL", "FILE-ID": "F",
                     "BATCH-COUNT": "1", "DETAIL-COUNT": "1"}),
        ])
        proc = self._run(5)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        # rejection still leaves a complete report
        report = json.loads((self.out / "audit_report.json").read_text())
        self.assertEqual(report["conclusion"], "business_rejected")

    def test_exit_two_when_evidence_incomplete(self):
        self._write([
            _rec(1, {"REC-TYPE": "FHEAD", "FILE-ID": "F"}),
            _rec(3, {"REC-TYPE": "FTAIL", "FILE-ID": "F",
                     "BATCH-COUNT": "0", "DETAIL-COUNT": "0"}),
        ])
        proc = self._run(3)           # physical line 2 has no evidence
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("cannot validate", proc.stderr)
        self.assertFalse(self.out.exists())

    def test_exit_two_on_bad_config(self):
        self.config_path.write_text("{not json", encoding="utf-8")
        self._write([_rec(1, {"REC-TYPE": "FHEAD"})])
        proc = self._run(1)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
