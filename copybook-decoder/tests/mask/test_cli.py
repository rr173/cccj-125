"""CLI tests for the mask subcommand."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]

KEY = "cli-test-key-material-0123456789"


def _rec(line):
    return {"meta": {"source_file": "f.dat", "line_number": line,
                     "byte_start": 0, "byte_end": 10, "layout": "L",
                     "layout_version": "1"},
            "fields": {"ACCT": f"100{line}"}}


class TestMaskCli(unittest.TestCase):

    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="mask-cli-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.input = self.tmp / "in.jsonl"
        with self.input.open("w", encoding="utf-8") as fh:
            for i in (1, 2):
                fh.write(json.dumps(_rec(i)) + "\n")
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(json.dumps({"fields": [
            {"path": ["ACCT"], "action": "pseudonym", "kind": "account",
             "group": "g"}]}), encoding="utf-8")

    def _run(self, *args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "copybook", "mask", *args],
            cwd=PROJECT, capture_output=True, text=True, env=full_env)

    def test_success_with_key_env(self):
        out = self.tmp / "out"
        with mock.patch.dict(os.environ, {"MASK_CLI_KEY": KEY}):
            proc = self._run(str(self.input), "--policy", str(self.policy),
                             "--key-env", "MASK_CLI_KEY",
                             "--out-dir", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("conclusion  : masked", proc.stdout)
        self.assertTrue((out / "in.masked.jsonl").is_file())
        self.assertTrue((out / "mask_report.json").is_file())
        self.assertTrue((out / "mask_report.txt").is_file())
        self.assertTrue((out / "record_report.jsonl").is_file())

    def test_success_with_key_file(self):
        key_file = self.tmp / "key.txt"
        key_file.write_text(KEY, encoding="utf-8")
        out = self.tmp / "out"
        proc = self._run(str(self.input), "--policy", str(self.policy),
                         "--key-file", str(key_file),
                         "--out-dir", str(out), "--chunk-size", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_key_env_fails(self):
        out = self.tmp / "out"
        with mock.patch.dict(os.environ, {}, clear=True):
            proc = self._run(str(self.input), "--policy", str(self.policy),
                             "--key-env", "MASK_CLI_UNSET",
                             "--out-dir", str(out))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not set", proc.stderr)
        self.assertFalse(out.exists())

    def test_key_source_required(self):
        proc = self._run(str(self.input), "--policy", str(self.policy),
                         "--out-dir", str(self.tmp / "out"))
        self.assertEqual(proc.returncode, 2)   # argparse usage error

    def test_bad_policy_fails(self):
        self.policy.write_text("{broken", encoding="utf-8")
        proc = self._run(str(self.input), "--policy", str(self.policy),
                         "--key-file", str(self.policy),
                         "--out-dir", str(self.tmp / "out"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_existing_output_fails(self):
        out = self.tmp / "out"
        out.mkdir()
        key_file = self.tmp / "key.txt"
        key_file.write_text(KEY, encoding="utf-8")
        proc = self._run(str(self.input), "--policy", str(self.policy),
                         "--key-file", str(key_file),
                         "--out-dir", str(out))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)


if __name__ == "__main__":
    unittest.main()
