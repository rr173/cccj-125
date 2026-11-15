"""CLI integration tests for the ``copybook impact`` exit-code contract."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.impact.builders import (NEW_V2, OLD_V1, build_main_scenario,
                                   entry, write_copybook, write_manifest)

PROJECT = Path(__file__).resolve().parents[2]


class CliImpactTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = self.dir / "report"

    def _run(self, old_manifest, new_manifest, out=None):
        return subprocess.run(
            [sys.executable, "-m", "copybook", "impact",
             "--old-manifest", str(old_manifest),
             "--new-manifest", str(new_manifest),
             "--out-dir", str(out or self.out)],
            cwd=PROJECT, capture_output=True, text=True)

    def test_exit_one_on_incompatible_change(self):
        old_manifest, new_manifest = build_main_scenario(self.dir)
        proc = self._run(old_manifest, new_manifest)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("incompatible", proc.stdout)
        self.assertTrue((self.out / "impact_report.json").exists())
        self.assertTrue((self.out / "impact_report.txt").exists())

    def test_exit_zero_on_compatible_change(self):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        layout = "      01  R.\n          05  A  PIC X(4).\n"
        view_added = ("      01  R.\n          05  A  PIC X(4).\n"
                      "          05  A-N REDEFINES A PIC 9(4).\n")
        write_copybook(old_dir, "ACCT-1.cpy", layout)
        write_copybook(new_dir, "ACCT-1.cpy", layout)
        write_copybook(new_dir, "ACCT-2.cpy", view_added)
        old_manifest = write_manifest(old_dir, [entry()])
        new_manifest = write_manifest(new_dir, [
            entry(effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01")])
        proc = self._run(old_manifest, new_manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("compatible", proc.stdout)

    def test_exit_two_on_unverifiable(self):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-2.cpy", NEW_V2)
        old_manifest = write_manifest(old_dir, [entry()])
        new_manifest = write_manifest(new_dir, [
            entry(effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-08-15")])  # overlap
        proc = self._run(old_manifest, new_manifest)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = json.loads((self.out / "impact_report.json").read_text())
        self.assertEqual(report["conclusion"], "unverifiable")

    def test_exit_two_on_existing_output_dir(self):
        self.out.mkdir()
        sentinel = self.out / "previous.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        old_manifest, new_manifest = build_main_scenario(self.dir)
        proc = self._run(old_manifest, new_manifest)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.out / "impact_report.json").exists())

    def test_decode_and_audit_cli_untouched(self):
        proc = subprocess.run([sys.executable, "-m", "copybook", "--help"],
                              cwd=PROJECT, capture_output=True, text=True)
        self.assertIn("decode", proc.stdout)
        self.assertIn("audit", proc.stdout)
        self.assertIn("impact", proc.stdout)


if __name__ == "__main__":
    unittest.main()
