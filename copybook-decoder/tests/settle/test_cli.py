"""CLI integration tests for the ``settle`` and ``trace`` subcommands."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from tests.settle.builders import (DEFAULT_CONFIG, bill, cancel_receipt,
                                   receipt, write_jsonl)


class SettleCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="settle-cli-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.input = self.tmp / "events.jsonl"
        self.config = self.tmp / "settle_config.json"
        self.config.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")

    def _write(self, rows):
        write_jsonl(self.input, rows)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "copybook", *args],
            cwd=PROJECT, capture_output=True, text=True)

    def _settle(self, out, cutoff="2026-06-01T00:00:00Z"):
        return self._run("settle", str(self.input), "--config",
                         str(self.config), "--cutoff", cutoff,
                         "--out-dir", str(out))

    def test_exit_zero_and_full_delivery(self):
        self._write([
            bill("b1", "2026-01-01T00:00:01Z", "B", "10.00", "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "10.00"),
        ])
        out = self.tmp / "result"
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("conclusion  : settled", proc.stdout)
        for name in ("settle_report.json", "events.jsonl", "objects.jsonl",
                     "allocations.jsonl"):
            self.assertTrue((out / name).is_file(), name)
        self.assertFalse((out / "invalid_groups.jsonl").exists())

    def test_exit_one_when_a_group_is_invalid_but_others_settle(self):
        self._write([
            bill("g1", "2026-01-01T00:00:01Z", "GB", "10.00", "2026-02-01",
                 account="GOOD"),
            receipt("g2", "2026-01-01T00:00:02Z", "GP", "10.00",
                    account="GOOD"),
            receipt("b1", "2026-01-01T00:00:03Z", "XP", "0.00",
                    account="BAD"),
        ])
        out = self.tmp / "result"
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("business_rejected", proc.stdout)
        self.assertTrue((out / "invalid_groups.jsonl").is_file())
        report = json.loads((out / "settle_report.json").read_text())
        self.assertEqual(report["valid_group_count"], 1)
        self.assertEqual(report["invalid_group_count"], 1)

    def test_exit_two_on_bad_json_and_no_output_dir(self):
        self.input.write_text('{"meta": {"x":1}\n{broken\n',
                              encoding="utf-8")
        out = self.tmp / "result"
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid JSON", proc.stderr)
        self.assertFalse(out.exists())

    def test_exit_two_on_duplicate_event_id(self):
        self._write([
            receipt("DUP", "2026-01-01T00:00:01Z", "P", "10.00"),
            bill("DUP", "2026-01-01T00:00:02Z", "B", "10.00", "2026-02-01"),
        ])
        out = self.tmp / "result"
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("duplicate event_id", proc.stderr)
        self.assertFalse(out.exists())

    def test_exit_two_when_output_dir_exists(self):
        out = self.tmp / "taken"
        out.mkdir()
        (out / "sentinel").write_text("keep", encoding="utf-8")
        self._write([receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00")])
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual((out / "sentinel").read_text(encoding="utf-8"),
                         "keep")

    def test_cutoff_equal_to_event_instant_is_inclusive(self):
        self._write([
            bill("b1", "2026-03-01T12:00:00Z", "B", "10.00", "2026-03-01"),
            receipt("r1", "2026-03-01T12:00:00Z", "P", "10.00"),
        ])
        out = self.tmp / "result"
        proc = self._settle(out, cutoff="2026-03-01T12:00:00Z")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        edges = [json.loads(line) for line in
                 (out / "allocations.jsonl").read_text().splitlines()]
        self.assertEqual(len(edges), 1)

    def test_trace_subcommand_reconstructs_history_from_result_only(self):
        self._write([
            bill("b1", "2026-01-01T00:00:01Z", "B", "10.00", "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "10.00"),
            cancel_receipt("c1", "2026-01-01T00:00:03Z", "P"),
        ])
        out = self.tmp / "result"
        proc = self._settle(out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        trace = self._run("trace", str(out), "P", "--kind", "RECEIPT")
        self.assertEqual(trace.returncode, 0, trace.stderr)
        payload = json.loads(trace.stdout)
        self.assertEqual(payload["object_id"], "P")
        self.assertEqual(len(payload["matches"][0]["allocation_history"]), 1)
        self.assertEqual(
            payload["matches"][0]["allocation_history"][0]["status"],
            "released")

    def test_trace_unknown_object_exit_two(self):
        self._write([receipt("r1", "2026-01-01T00:00:02Z", "P", "10.00")])
        out = self.tmp / "result"
        self.assertEqual(self._settle(out).returncode, 0)
        trace = self._run("trace", str(out), "GHOST")
        self.assertEqual(trace.returncode, 2)
        self.assertIn("GHOST", trace.stderr)

    def test_legacy_subcommands_remain_registered(self):
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0)
        for name in ("decode", "audit", "impact", "mask", "settle",
                     "trace"):
            self.assertIn(name, proc.stdout)


if __name__ == "__main__":
    unittest.main()
