"""CLI integration tests for admit / admit-explain / admit-table."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from tests.relation.builders import field, ref, table, wrap, write_contract, \
    write_jsonl


class AdmitCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-cli-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.out = self.tmp / "out"
        self.stage = self.tmp / "stage"

    def _p(self, name):
        return str(self.data / name)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "copybook", *args],
            cwd=PROJECT, capture_output=True, text=True)

    def _contract(self):
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "k": "K"})])
        write_jsonl(self.data / "d.jsonl", [
            wrap("D1", {"id": "D1", "fk": "K", "miss": None},
                 line_number=1),
            wrap("D2", {"id": "D2", "fk": "GHOST", "miss": None},
                 line_number=2)])
        write_jsonl(self.data / "e.jsonl", [])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "k": field(["k"])},
                  candidate_keys={"kp": ["k"]}),
            table("D", [self._p("d.jsonl")],
                  {"id": field(["id"]), "fk": field(["fk"]),
                   "miss": field(["miss"], nullable=True)},
                  candidate_keys={"kd": ["id"]},
                  references=[
                      ref("r_p", ["fk"], "P", "kp", "mandatory")]),
            table("E", [self._p("e.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]})]
        cp = self.tmp / "contract.json"
        write_contract(cp, contract)
        return cp

    def test_exit_codes_and_delivery_layout(self):
        cp = self._contract()
        proc = self._run("admit", "--contract", str(cp),
                         "--out-dir", str(self.out),
                         "--temp-dir", str(self.stage))
        self.assertEqual(proc.returncode, 1, proc.stderr)  # business reject
        self.assertIn("business_rejected", proc.stdout)
        marker = self.out / "COMPLETE.json"
        self.assertTrue(marker.is_file())
        report = json.loads((self.out / "admission_report.json").read_text())
        counts = report["counts"]
        self.assertEqual(counts["P"]["kept"], 1)
        self.assertEqual(counts["D"]["cascade_reject"], 1)
        self.assertEqual(counts["D"]["kept"], 1)
        # Every identity has exactly one disposition and counts reconcile.
        for tname, c in counts.items():
            self.assertEqual(c["input"],
                             c["kept"] + c["local_reject"]
                             + c["cascade_reject"], tname)

    def test_explain_kept_rejected_and_proof_node(self):
        cp = self._contract()
        self.assertEqual(self._run("admit", "--contract", str(cp),
                                   "--out-dir", str(self.out),
                                   "--temp-dir", str(self.stage)).returncode,
                         1)
        ok = self._run("admit-explain", str(self.out), "D", "D1")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload["status"], "kept")
        self.assertEqual(payload["witnesses"][0]["parent_row_id"], "P1")
        bad = self._run("admit-explain", str(self.out), "D", "D2")
        self.assertEqual(bad.returncode, 0)
        rejected = json.loads(bad.stdout)
        self.assertEqual(rejected["status"], "cascade_reject")
        proof_id = rejected["primary_proof"]["proof_id"]
        node = self._run("admit-explain", str(self.out), "D", "D2",
                         "--proof-id", str(proof_id))
        self.assertEqual(node.returncode, 0, node.stderr)
        self.assertEqual(json.loads(node.stdout)["proof_id"], proof_id)

    def test_admit_table_streams_sorted_kept_rows(self):
        cp = self._contract()
        self.assertEqual(self._run("admit", "--contract", str(cp),
                                   "--out-dir", str(self.out),
                                   "--temp-dir", str(self.stage)).returncode,
                         1)
        proc = self._run("admit-table", str(self.out), "P")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ids = [json.loads(line)["fields"]["id"]
               for line in proc.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(ids, ["P1"])
        missing = self._run("admit-table", str(self.out), "NOPE")
        self.assertEqual(missing.returncode, 2)

    def test_bad_contract_exit_two_and_no_marker(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"tables": []}), encoding="utf-8")
        proc = self._run("admit", "--contract", str(bad),
                         "--out-dir", str(self.out),
                         "--temp-dir", str(self.stage))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.out.exists())

    def test_bad_json_exit_two_without_completed_delivery(self):
        cp = self._contract()
        (self.data / "d.jsonl").write_text("{not json\n", encoding="utf-8")
        proc = self._run("admit", "--contract", str(cp),
                         "--out-dir", str(self.out),
                         "--temp-dir", str(self.stage))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid JSON", proc.stderr)
        self.assertFalse((self.out / "COMPLETE.json").exists())

    def test_explain_without_marker_is_refused(self):
        cp = self._contract()
        self.out.mkdir()
        proc = self._run("admit-explain", str(self.out), "D", "D1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a completed", proc.stderr)

    def test_legacy_subcommands_remain_registered(self):
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0)
        for name in ("decode", "audit", "impact", "mask", "settle",
                     "trace", "admit", "admit-explain", "admit-table"):
            self.assertIn(name, proc.stdout)


if __name__ == "__main__":
    unittest.main()
