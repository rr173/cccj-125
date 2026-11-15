"""Field, key, type and null-rule boundary tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from copybook.relation import (CASCADE_REJECT, KEPT, LOCAL_REJECT,
                               RelationContract, explain_row, run_admission)
from tests.relation.builders import (field, ref, table, wrap,
                                     write_contract, write_jsonl)


class BoundaryCase(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-bound-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.jobs = 0

    def _p(self, name):
        return str(self.data / name)

    def run_job(self, tables):
        cp = write_contract(self.tmp / f"c{self.jobs}.json", tables)
        out = self.tmp / f"out{self.jobs}"
        self.jobs += 1
        report = run_admission(contract=RelationContract.load(cp),
                               output_dir=out,
                               temp_dir=self.tmp / "stage")
        return out, report

    def explain(self, out, t, rid):
        return explain_row(out, t, rid)


class TestTypesAndKeys(BoundaryCase):
    def test_string_007_differs_from_integer_7(self):
        # References require matching declared types, so the no-conversion
        # guarantee is shown on string columns: parent "007" matches only a
        # child carrying exactly "007", never "7" (no trimming/numeric
        # coercion), and a child integer column simply cannot reference a
        # string key at contract time.
        write_jsonl(self.data / "p.jsonl", [
            wrap("PS", {"id": "PS", "code": "007"})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("CS", {"id": "CS", "s": "7"}),
            wrap("CG", {"id": "CG", "s": "007"})])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "code": field(["code"])},
                  candidate_keys={"kp": ["code"]}),
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]), "s": field(["s"])},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("r_p", ["s"], "P", "kp")])]
        out, _ = self.run_job(contract)
        self.assertEqual(self.explain(out, "C", "CS")["status"],
                         CASCADE_REJECT)
        self.assertEqual(self.explain(out, "C", "CG")["status"], KEPT)

    def test_integers_beyond_64_bits_preserved_exactly(self):
        big = 170141183460469231731687303715884105727   # 2**127 - 1
        neg = -big
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "n": big}),
            wrap("P2", {"id": "P2", "n": neg})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("C1", {"id": "C1", "n": big}),
            wrap("C2", {"id": "C2", "n": neg})])
        f = {"id": field(["id"]), "n": field(["n"], "integer")}
        contract = [
            table("P", [self._p("p.jsonl")], f,
                  candidate_keys={"kp": ["n"]}),
            table("C", [self._p("c.jsonl")], f,
                  candidate_keys={"kc": ["id"]},
                  references=[ref("r_p", ["n"], "P", "kp")])]
        out, _ = self.run_job(contract)
        for rid in ("C1", "C2"):
            d = self.explain(out, "C", rid)
            self.assertEqual(d["status"], KEPT, rid)
            self.assertEqual(d["record"]["fields"]["n"],
                             big if rid == "C1" else neg)

    def test_boolean_is_not_an_integer_or_string(self):
        write_jsonl(self.data / "t.jsonl", [
            wrap("T1", {"id": "T1", "n": True}),
            wrap("T2", {"id": "T2", "n": False}),
            wrap("T3", {"id": "T3", "n": 1}),
            wrap("T4", {"id": "T4", "s": True})])
        contract = [
            table("T", [self._p("t.jsonl")],
                  {"id": field(["id"]),
                   "n": field(["n"], "integer", nullable=True),
                   "s": field(["s"], "string", nullable=True)},
                  candidate_keys={"kt": ["id"]})]
        out, _ = self.run_job(contract)
        for rid, ok in (("T1", False), ("T2", False), ("T3", True),
                        ("T4", False)):
            d = self.explain(out, "T", rid)
            self.assertEqual(d["status"], KEPT if ok else LOCAL_REJECT, rid)
            if not ok:
                self.assertTrue(
                    any(x["code"] == "type_mismatch"
                        for x in d["local_diagnostics"]))

    def test_composite_key_matches_componentwise(self):
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "a": "x", "n": 1}),
            wrap("P2", {"id": "P2", "a": "x", "n": 2})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("CG", {"id": "CG", "fa": "x", "fn": 1}),
            wrap("CB", {"id": "CB", "fa": "x", "fn": 9})])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "a": field(["a"]),
                   "n": field(["n"], "integer")},
                  candidate_keys={"kp": ["a", "n"]}),
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]), "fa": field(["fa"]),
                   "fn": field(["fn"], "integer")},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("r_p", ["fa", "fn"], "P", "kp")])]
        out, _ = self.run_job(contract)
        self.assertEqual(self.explain(out, "C", "CG")["status"], KEPT)
        cb = self.explain(out, "C", "CB")
        self.assertEqual(cb["status"], CASCADE_REJECT)
        self.assertEqual(
            [c["value"] for c in cb["primary_proof"]["key_tuple"]],
            ["x", 9])

    def test_optional_all_null_skipped_half_null_rejected(self):
        # Two-component optional reference against a composite candidate key.
        write_jsonl(self.data / "p.jsonl",
                    [wrap("P1", {"id": "P1", "a": "K", "b": "K"})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("ALL", {"id": "ALL", "a": None, "b": None}),
            wrap("HALF", {"id": "HALF", "a": "K", "b": None}),
            wrap("FULL", {"id": "FULL", "a": "K", "b": "K"})])
        p_fields = {"id": field(["id"]), "a": field(["a"]),
                    "b": field(["b"])}
        c_fields = {"id": field(["id"]),
                    "a": field(["a"], nullable=True),
                    "b": field(["b"], nullable=True)}
        contract = [
            table("P", [self._p("p.jsonl")], p_fields,
                  candidate_keys={"kp": ["a", "b"]}),
            table("C", [self._p("c.jsonl")], c_fields,
                  candidate_keys={"kc": ["id"]},
                  references=[
                      ref("opt_pair", ["a", "b"], "P", "kp",
                          "optional")])]
        out, _ = self.run_job(contract)
        allrow = self.explain(out, "C", "ALL")
        half = self.explain(out, "C", "HALF")
        full = self.explain(out, "C", "FULL")
        self.assertEqual(allrow["status"], KEPT)
        self.assertEqual(allrow["all_null_optional_references"],
                         ["opt_pair"])
        self.assertEqual(half["status"], LOCAL_REJECT)
        self.assertTrue(any(x["code"] == "partial_null_reference"
                            for x in half["local_diagnostics"]))
        # FULL: the optional reference has a real match and a witness.
        self.assertEqual(full["status"], KEPT)
        self.assertEqual(
            [w["reference"] for w in full["witnesses"]], ["opt_pair"])

    def test_mandatory_null_is_unsupported_not_local_field_error(self):
        write_jsonl(self.data / "p.jsonl", [wrap("P1", {"id": "P1"})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("C1", {"id": "C1", "f": None})])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"kp": ["id"]}),
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]),
                   "f": field(["f"], nullable=True)},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("man", ["f"], "P", "kp", "mandatory")])]
        out, _ = self.run_job(contract)
        c = self.explain(out, "C", "C1")
        self.assertEqual((c["status"], c["layer"]),
                         (CASCADE_REJECT, 1))

    def test_target_key_illegal_row_is_not_a_candidate(self):
        # Parent's key column is null/typed wrong: it never poses as a
        # candidate for the child's known tuple.
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "k": "K", "n": "not-an-int"})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("C1", {"id": "C1", "f": 3})])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "k": field(["k"]),
                   "n": field(["n"], "integer")},
                  candidate_keys={"kp": ["n"]}),
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]),
                   "f": field(["f"], "integer")},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("r_p", ["f"], "P", "kp")])]
        out, _ = self.run_job(contract)
        p = self.explain(out, "P", "P1")
        c = self.explain(out, "C", "C1")
        self.assertEqual(p["status"], LOCAL_REJECT)
        self.assertEqual(c["primary_proof"]["initial_candidates"], [])

    def test_self_reference_kept_when_it_points_at_itself(self):
        write_jsonl(self.data / "t.jsonl", [
            wrap("S1", {"id": "S1", "self": "S1"}),
            wrap("S2", {"id": "S2", "self": "NOPE"})])
        contract = [
            table("T", [self._p("t.jsonl")],
                  {"id": field(["id"]), "self": field(["self"])},
                  candidate_keys={"kt": ["id"]},
                  references=[ref("r_self", ["self"], "T", "kt")])]
        out, _ = self.run_job(contract)
        self.assertEqual(self.explain(out, "T", "S1")["status"], KEPT)
        s2 = self.explain(out, "T", "S2")
        self.assertEqual(s2["status"], CASCADE_REJECT)

    def test_undeclared_fields_are_carried_verbatim(self):
        weird = {"id": "W1", "declared": "d",
                 "weird.name.with.dots": {"quote'key": 'v"alue'},
                 "nested": [1, {"x": 2}]}
        write_jsonl(self.data / "t.jsonl", [
            wrap(None, weird, rid_field="__never_injected__")])
        contract = [
            table("T", [self._p("t.jsonl")],
                  {"id": field(["id"]),
                   "declared": field(["declared"])},
                  candidate_keys={"kt": ["id"]})]
        out, _ = self.run_job(contract)
        d = self.explain(out, "T", "W1")
        self.assertEqual(d["record"]["fields"]["weird.name.with.dots"],
                         weird["weird.name.with.dots"])
        self.assertEqual(d["record"]["fields"]["nested"], weird["nested"])

    def test_strings_keep_whitespace_and_case(self):
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "k": "  AbC "})])
        write_jsonl(self.data / "c.jsonl", [
            wrap("C1", {"id": "C1", "f": "  AbC "}),
            wrap("C2", {"id": "C2", "f": "abc"})])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "k": field(["k"])},
                  candidate_keys={"kp": ["k"]}),
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]), "f": field(["f"])},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("r", ["f"], "P", "kp")])]
        out, _ = self.run_job(contract)
        self.assertEqual(self.explain(out, "C", "C1")["status"], KEPT)
        self.assertEqual(self.explain(out, "C", "C2")["status"],
                         CASCADE_REJECT)

    def test_special_characters_in_names_and_key_values(self):
        # Dots, quotes and separators are data, never SQL/string fragments.
        rid = "row.1'\"|;"
        kval = 'k"ey.va|lue'
        write_jsonl(self.data / "t.jsonl", [
            wrap(None, {"id": rid, "weird.col": kval},
                 rid_field="__never_injected__")])
        contract = [
            table("we.ird\"table|x", [self._p("t.jsonl")],
                  {"id": field(["id"]),
                   "weird.col": field(["weird.col"])},
                  candidate_keys={"k.weird'|k": ["weird.col"]})]
        out, _ = self.run_job(contract)
        d = explain_row(out, "we.ird\"table|x", rid)
        self.assertEqual(d["status"], KEPT)


class TestMissingAndNullColumns(BoundaryCase):
    def test_missing_non_nullable_column_is_local_reject(self):
        write_jsonl(self.data / "t.jsonl", [wrap("M1", {"id": "M1"})])
        contract = [
            table("T", [self._p("t.jsonl")],
                  {"id": field(["id"]),
                   "need": field(["need"])},
                  candidate_keys={"kt": ["id"]})]
        out, _ = self.run_job(contract)
        d = self.explain(out, "T", "M1")
        self.assertEqual(d["status"], LOCAL_REJECT)
        self.assertEqual(d["local_diagnostics"][0]["code"],
                         "null_or_missing_column")

    def test_empty_table_and_zero_row_file_are_accepted(self):
        write_jsonl(self.data / "empty.jsonl", [])
        contract = [
            table("E", [self._p("empty.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]})]
        out, report = self.run_job(contract)
        self.assertEqual(report["conclusion"], "admitted")
        self.assertEqual(report["counts"]["E"]["input"], 0)
        self.assertTrue(
            (out / "tables").glob("*/kept.jsonl"))


if __name__ == "__main__":
    unittest.main()
