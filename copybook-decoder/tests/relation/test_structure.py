"""Structural input faults and contract faults.

Structural faults make the whole handoff unprocessable: the run raises
RelationInputError (or RelationConfigError for a bad contract), no delivery
directory or completion marker is left, and the condition is never treated
as one ordinary bad business row.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from copybook.relation import (RelationConfigError, RelationContract,
                               RelationInputError, run_admission)
from tests.relation.builders import field, table, wrap, write_contract, \
    write_jsonl


class StructuralCase(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-struct-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.out = self.tmp / "out"

    def _p(self, name):
        return str(self.data / name)

    def _simple_contract(self, file_name="t.jsonl"):
        return [table("T", [self._p(file_name)],
                      {"id": field(["id"])}, candidate_keys={"kt": ["id"]})]

    def _run(self, tables):
        cp = write_contract(self.tmp / "contract.json", tables)
        return run_admission(contract=RelationContract.load(cp),
                             output_dir=self.out,
                             temp_dir=self.tmp / "stage")

    def _assert_no_delivery(self):
        self.assertFalse(self.out.exists())
        # Only this run's unique staging subdir is created and cleaned.
        self.assertFalse(any((self.tmp / "stage").glob("relation-staging-*")))

    def test_bad_json_aborts_without_delivery(self):
        (self.data / "t.jsonl").write_text(
            json.dumps(wrap("T1")) + "\n{broken json\n", encoding="utf-8")
        with self.assertRaises(RelationInputError) as ctx:
            self._run(self._simple_contract())
        self.assertIn("invalid JSON", str(ctx.exception))
        self._assert_no_delivery()

    def test_duplicate_json_members_abort(self):
        (self.data / "t.jsonl").write_text(
            '{"meta": {"source_file": "t.jsonl", "line_number": 1, '
            '"byte_start": 0, "byte_end": 1}, "fields": {"id": "a"}, '
            '"fields": {"id": "b"}}\n', encoding="utf-8")
        with self.assertRaises(RelationInputError) as ctx:
            self._run(self._simple_contract())
        self.assertIn("duplicate JSON object member", str(ctx.exception))
        self._assert_no_delivery()

    def test_missing_row_id_aborts(self):
        write_jsonl(self.data / "t.jsonl",
                    [wrap(None, {"other": 1}, rid_field="__x__")])
        with self.assertRaises(RelationInputError):
            self._run(self._simple_contract())
        self._assert_no_delivery()

    def test_duplicate_row_id_aborts_even_when_identical(self):
        row = wrap("DUP", {"id": "DUP"}, line_number=1)
        write_jsonl(self.data / "t.jsonl", [row, dict(row)])
        with self.assertRaises(RelationInputError) as ctx:
            self._run(self._simple_contract())
        self.assertIn("duplicate row_id", str(ctx.exception))
        self._assert_no_delivery()

    def test_duplicate_row_id_across_files_of_one_table_aborts(self):
        write_jsonl(self.data / "a.jsonl", [wrap("DUP", line_number=1)])
        write_jsonl(self.data / "b.jsonl", [wrap("DUP", source_file="b.jsonl",
                                                 line_number=1)])
        contract = [table("T", [self._p("a.jsonl"), self._p("b.jsonl")],
                          {"id": field(["id"])},
                          candidate_keys={"kt": ["id"]})]
        with self.assertRaises(RelationInputError):
            self._run(contract)
        self._assert_no_delivery()

    def test_invalid_meta_shapes_abort(self):
        base = wrap("T1")
        bad_meta_variants = [
            ("missing source_file",
             {"line_number": 1, "byte_start": 0, "byte_end": 1}),
            ("empty source_file",
             {"source_file": "", "line_number": 1, "byte_start": 0,
              "byte_end": 1}),
            ("zero line",
             {"source_file": "t.jsonl", "line_number": 0, "byte_start": 0,
              "byte_end": 1}),
            ("bool line",
             {"source_file": "t.jsonl", "line_number": True,
              "byte_start": 0, "byte_end": 1}),
            ("bool offset",
             {"source_file": "t.jsonl", "line_number": 1,
              "byte_start": False, "byte_end": 1}),
            ("reversed range",
             {"source_file": "t.jsonl", "line_number": 1,
              "byte_start": 5, "byte_end": 5}),
            ("negative end",
             {"source_file": "t.jsonl", "line_number": 1,
              "byte_start": 0, "byte_end": -1})]
        for label, meta in bad_meta_variants:
            (self.data / "t.jsonl").write_text(
                json.dumps({"meta": meta, "fields": {"id": "T1"}}) + "\n",
                encoding="utf-8")
            with self.subTest(label=label):
                with self.assertRaises(RelationInputError):
                    self._run(self._simple_contract())
                self.assertFalse(self.out.exists())

    def test_invalid_wrapper_aborts(self):
        (self.data / "t.jsonl").write_text(
            json.dumps({"meta": {"source_file": "t.jsonl",
                                 "line_number": 1, "byte_start": 0,
                                 "byte_end": 1}}) + "\n", encoding="utf-8")
        with self.assertRaises(RelationInputError):
            self._run(self._simple_contract())
        self._assert_no_delivery()

    def test_blank_line_aborts(self):
        (self.data / "t.jsonl").write_text(
            json.dumps(wrap("T1")) + "\n\n", encoding="utf-8")
        with self.assertRaises(RelationInputError):
            self._run(self._simple_contract())
        self._assert_no_delivery()

    def test_output_dir_refuses_overwrite(self):
        write_jsonl(self.data / "t.jsonl", [wrap("T1")])
        self._run(self._simple_contract())
        self.assertTrue((self.out / "COMPLETE.json").is_file())
        with self.assertRaises(RelationInputError) as ctx:
            self._run(self._simple_contract())
        self.assertIn("already exists", str(ctx.exception))

    def test_unreadable_file_aborts(self):
        contract = [table("T", [self._p("missing.jsonl")],
                          {"id": field(["id"])},
                          candidate_keys={"kt": ["id"]})]
        with self.assertRaises(RelationInputError):
            self._run(contract)
        self._assert_no_delivery()

    def test_file_listed_for_two_tables_aborts(self):
        write_jsonl(self.data / "shared.jsonl", [wrap("S1")])
        contract = [
            table("T1", [self._p("shared.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"k1": ["id"]}),
            table("T2", [self._p("shared.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"k2": ["id"]})]
        with self.assertRaises(RelationInputError):
            self._run(contract)
        self._assert_no_delivery()


class TestContractFaults(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-cfg-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)

    def _expect(self, data):
        with self.assertRaises(RelationConfigError):
            RelationContract.from_dict(data)

    def test_basic_shape_faults(self):
        self._expect({})
        self._expect({"tables": []})
        self._expect({"tables": [{}]})
        self._expect({"tables": [{"name": "T", "files": [],
                                  "row_id_path": ["id"], "fields": {
                                      "id": {"path": ["id"],
                                             "type": "string"}}}]})

    def test_bad_field_type_and_path(self):
        self._expect({"tables": [{"name": "T", "files": ["f"],
                                  "row_id_path": ["id"],
                                  "fields": {"a": {"path": ["a"],
                                                  "type": "number"}}}]})
        self._expect({"tables": [{"name": "T", "files": ["f"],
                                  "row_id_path": ["id"],
                                  "fields": {"a": {"path": [],
                                                  "type": "string"}}}]})

    def test_reference_uses_undeclared_column(self):
        self._expect({"tables": [
            {"name": "T", "files": ["f"], "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"}},
             "candidate_keys": {"k": ["id"]},
             "references": [{"name": "r", "columns": ["ghost"],
                             "target_table": "T", "target_key": "k",
                             "rule": "mandatory"}]}]})

    def test_candidate_key_on_nullable_column_rejected(self):
        self._expect({"tables": [
            {"name": "T", "files": ["f"], "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string",
                               "nullable": True}},
             "candidate_keys": {"k": ["id"]}}]})

    def test_unknown_target_table_and_key(self):
        good = {"name": "T", "files": ["f"], "row_id_path": ["id"],
                "fields": {"id": {"path": ["id"], "type": "string"}},
                "candidate_keys": {"k": ["id"]}, "references": []}
        ref_ghost_table = [{"name": "r", "columns": ["id"],
                            "target_table": "GHOST", "target_key": "k",
                            "rule": "mandatory"}]
        ref_ghost_key = [{"name": "r", "columns": ["id"],
                          "target_table": "T", "target_key": "ghost",
                          "rule": "mandatory"}]
        self._expect(
            {"tables": [dict(good, references=ref_ghost_table)]})
        self._expect(
            {"tables": [dict(good, references=ref_ghost_key)]})

    def test_arity_and_type_mismatch_rejected(self):
        two = {"name": "P", "files": ["p"], "row_id_path": ["id"],
               "fields": {"a": {"path": ["a"], "type": "string"},
                          "n": {"path": ["n"], "type": "integer"}},
               "candidate_keys": {"kp": ["a", "n"]}, "references": []}
        child = {"name": "C", "files": ["c"], "row_id_path": ["id"],
                 "fields": {"id": {"path": ["id"], "type": "string"},
                            "n": {"path": ["n"], "type": "integer"}},
                 "candidate_keys": {"kc": ["id"]},
                 "references": [{"name": "r", "columns": ["n"],
                                 "target_table": "P", "target_key": "kp",
                                 "rule": "mandatory"}]}
        self._expect({"tables": [two, child]})  # arity 1 vs 2
        child2 = dict(child, references=[
            {"name": "r", "columns": ["id"], "target_table": "P",
             "target_key": "kp", "rule": "mandatory"}])  # string vs tuple
        self._expect({"tables": [two, child2]})

    def test_bad_rule_and_duplicate_names(self):
        base = {"name": "T", "files": ["f"], "row_id_path": ["id"],
                "fields": {"id": {"path": ["id"], "type": "string"}},
                "candidate_keys": {"k": ["id"]}}
        self._expect({"tables": [
            dict(base, references=[{"name": "r", "columns": ["id"],
                                    "target_table": "T", "target_key": "k",
                                    "rule": "sometimes"}])]})
        self._expect({"tables": [
            dict(base, references=[
                {"name": "r", "columns": ["id"], "target_table": "T",
                 "target_key": "k", "rule": "mandatory"},
                {"name": "r", "columns": ["id"], "target_table": "T",
                 "target_key": "k", "rule": "optional"}])]})

    def test_duplicate_table_name_rejected(self):
        t = {"name": "T", "files": ["f"], "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"}},
             "candidate_keys": {"k": ["id"]}}
        self._expect({"tables": [t, t]})


if __name__ == "__main__":
    unittest.main()
