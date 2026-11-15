"""Delivery atomicity: a write failure leaves no completed-looking result."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from copybook.relation import RelationContract, run_admission
from copybook.relation import engine as engine_mod
from tests.relation.builders import field, table, wrap, write_contract, \
    write_jsonl


class DeliveryAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-atom-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def _contract(self):
        write_jsonl(self.data / "t.jsonl", [
            wrap("T1", {"id": "T1", "gone": "x"}, line_number=1)])
        write_jsonl(self.data / "e.jsonl", [])
        tables = [
            table("T", [str(self.data / "t.jsonl")],
                  {"id": field(["id"]), "gone": field(["gone"])},
                  candidate_keys={"kt": ["id"]},
                  references=[{"name": "r_gone", "columns": ["gone"],
                               "target_table": "E", "target_key": "ke",
                               "rule": "mandatory"}]),
            table("E", [str(self.data / "e.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]})]
        return write_contract(self.tmp / "c.json", tables)

    def test_marker_write_failure_removes_half_written_delivery(self):
        out = self.tmp / "out"
        contract = RelationContract.load(self._contract())
        real_write_text = Path.write_text

        def fail_marker(self, data, *a, **k):
            if self.name == "COMPLETE.json":
                raise OSError("simulated marker write failure")
            return real_write_text(self, data, *a, **k)

        with mock.patch.object(Path, "write_text", autospec=True,
                               side_effect=fail_marker):
            with self.assertRaises(OSError):
                run_admission(contract=contract, output_dir=out,
                              temp_dir=self.tmp / "stage")
        # No completed delivery may remain; the half-written dir is removed.
        self.assertFalse((out / "COMPLETE.json").exists())
        self.assertFalse(out.exists())
        # And only this run's staging subdir was cleaned.
        self.assertFalse(
            any((self.tmp / "stage").glob("relation-staging-*")))

    def test_record_file_write_failure_removes_delivery(self):
        out = self.tmp / "out2"
        contract = RelationContract.load(self._contract())

        def boom(*a, **k):
            raise OSError("simulated jsonl write failure")

        with mock.patch.object(engine_mod, "_write_record_file",
                               side_effect=boom):
            with self.assertRaises(OSError):
                run_admission(contract=contract, output_dir=out,
                              temp_dir=self.tmp / "stage2")
        self.assertFalse(out.exists())

    def test_input_readonly_inputs_are_not_modified(self):
        # The job must never write back into the input files.
        contract_path = self._contract()
        src = self.data / "t.jsonl"
        self.assertTrue(src.is_file())
        before = src.read_bytes()
        out = self.tmp / "out3"
        contract = RelationContract.load(contract_path)
        run_admission(contract=contract, output_dir=out,
                      temp_dir=self.tmp / "stage3")
        self.assertEqual(src.read_bytes(), before)
        self.assertTrue((out / "COMPLETE.json").is_file())


if __name__ == "__main__":
    unittest.main()
