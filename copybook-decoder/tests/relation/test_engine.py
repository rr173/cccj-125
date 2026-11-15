"""Core engine tests: the hand-written scenarios and all fixed-point rules.

These tests exercise the production engine end to end (contract -> staging
-> elimination -> proofs -> delivery) and assert the exact verdicts,
layers, witnesses and proof shapes required by the specification.  A
separate independent oracle (tests/relation/test_oracle.py) re-derives the
same result without calling any production decision code.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from copybook.relation import (CASCADE_REJECT, KEPT, LOCAL_REJECT,
                               RelationContract, explain_row,
                               get_proof_node, run_admission)
from tests.relation.builders import (field, ref, table, wrap,
                                     write_contract, write_jsonl)


class AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="relation-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.temp_dir = self.tmp / "staging"
        self._job_count = 0

    def _p(self, name: str) -> str:
        return str(self.data / name)

    def run_job(self, tables: list[dict]):
        contract_path = write_contract(self.tmp / "contract.json", tables)
        out = self.tmp / f"out-{self._job_count}"
        self._job_count += 1
        report = run_admission(
            contract=RelationContract.load(contract_path),
            output_dir=out, temp_dir=self.temp_dir)
        return out, report

    def explain(self, out, table_name, row_id):
        return explain_row(out, table_name, row_id)


class TestClosedLoopAndCascade(AdmissionCase):
    """A1<->B1 kept; X1<->Y1 with Y1 also pointing at empty table Z."""

    def _build(self):
        write_jsonl(self.data / "a.jsonl", [wrap("A1", {"ref": "A1"},
                                                  source_file="a.jsonl")])
        write_jsonl(self.data / "b.jsonl", [wrap("B1", {"ref": "B1"},
                                                  source_file="b.jsonl")])
        write_jsonl(self.data / "x.jsonl", [wrap("X1", {"yref": "Y1"},
                                                  source_file="x.jsonl")])
        write_jsonl(self.data / "y.jsonl",
                    [wrap("Y1", {"xref": "X1", "zref": "ZGONE"},
                          source_file="y.jsonl")])
        write_jsonl(self.data / "z.jsonl", [])   # explicit empty table
        return [
            table("A", [self._p("a.jsonl")],
                  {"id": field(["id"]), "ref": field(["ref"])},
                  candidate_keys={"ka": ["id"]},
                  references=[ref("r_self", ["ref"], "A", "ka")]),
            table("B", [self._p("b.jsonl")],
                  {"id": field(["id"]), "ref": field(["ref"])},
                  candidate_keys={"kb": ["id"]},
                  references=[ref("r_self", ["ref"], "B", "kb")]),
            table("X", [self._p("x.jsonl")],
                  {"id": field(["id"]), "yref": field(["yref"])},
                  candidate_keys={"kx": ["id"]},
                  references=[ref("r_y", ["yref"], "Y", "ky")]),
            table("Y", [self._p("y.jsonl")],
                  {"id": field(["id"]), "xref": field(["xref"]),
                   "zref": field(["zref"])},
                  candidate_keys={"ky": ["id"]},
                  references=[ref("r_x", ["xref"], "X", "kx"),
                              ref("r_z", ["zref"], "Z", "kz")]),
            table("Z", [self._p("z.jsonl")],
                  {"id": field(["id"])},
                  candidate_keys={"kz": ["id"]})]

    def test_handwritten_expected_verdicts(self):
        out, report = self.run_job(self._build())
        self.assertEqual(self.explain(out, "A", "A1")["status"], KEPT)
        self.assertEqual(self.explain(out, "B", "B1")["status"], KEPT)
        y = self.explain(out, "Y", "Y1")
        x = self.explain(out, "X", "X1")
        self.assertEqual((y["status"], y["layer"]), (CASCADE_REJECT, 1))
        self.assertEqual((x["status"], x["layer"]), (CASCADE_REJECT, 2))
        # Y's primary proof is the reference with no initial candidate.
        self.assertEqual(y["primary_proof"]["reference"], "r_z")
        self.assertEqual(y["primary_proof"]["initial_candidates"], [])
        # X's proof points at Y1, whose own proof node is referenced.
        cands = x["primary_proof"]["initial_candidates"]
        self.assertEqual([c["parent_row_id"] for c in cands], ["Y1"])
        self.assertEqual(cands[0]["parent_layer"], 1)
        self.assertIsNotNone(cands[0]["parent_proof_id"])
        self.assertEqual(report["counts"]["Z"],
                         {"input": 0, "kept": 0, "local_reject": 0,
                          "cascade_reject": 0})


class TestSameKeyCandidates(AdmissionCase):
    """D points at one key carried by P1 and P2."""

    def _contract(self, p2_local_bad: bool):
        return [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "pk": field(["pk"]),
                   "broken": field(["broken"], nullable=not p2_local_bad)},
                  candidate_keys={"kp": ["pk"]},
                  references=([ref("r_missing", ["pk"], "P", "kp")]
                              if not p2_local_bad else [])),
            table("D", [self._p("d.jsonl")],
                  {"did": field(["did"]), "fk": field(["fk"])},
                  row_id_path=["did"],
                  candidate_keys={"kd": ["did"]},
                  references=[ref("r_p", ["fk"], "P", "kp")])]

    def _write_rows(self, p2_broken_null: bool):
        p1 = wrap("P1", {"id": "P1", "pk": "K1", "broken": None},
                  source_file="p.jsonl", line_number=1)
        p2_fields = {"id": "P2", "pk": "K1"}
        if p2_broken_null:
            p2_fields["broken"] = None
        p2 = wrap("P2", p2_fields, source_file="p.jsonl", line_number=2)
        write_jsonl(self.data / "p.jsonl", [p1, p2])
        write_jsonl(self.data / "d.jsonl",
                    [wrap("D1", {"did": "D1", "fk": "K1"},
                          source_file="d.jsonl", rid_field="did")])

    def test_one_local_bad_one_valid_transaction_kept_with_p2_witness(self):
        # broken non-nullable: P1 null => local reject; P2 omits it and the
        # column is... here we force non-nullable, so give P2 a value.
        self._write_rows(p2_broken_null=False)
        # Patch P1/P2 directly to make P2 locally valid:
        rows = [json.loads(l) for l in
                (self.data / "p.jsonl").read_text().splitlines()]
        rows[1]["fields"]["broken"] = "ok"
        write_jsonl(self.data / "p.jsonl", rows)
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "pk": field(["pk"]),
                   "broken": field(["broken"])},
                  candidate_keys={"kp": ["pk"]}),
            table("D", [self._p("d.jsonl")],
                  {"did": field(["did"]), "fk": field(["fk"])},
                  row_id_path=["did"], candidate_keys={"kd": ["did"]},
                  references=[ref("r_p", ["fk"], "P", "kp")])]
        out, _ = self.run_job(contract)
        d = self.explain(out, "D", "D1")
        self.assertEqual(d["status"], KEPT)
        self.assertEqual(d["witnesses"], [
            {"reference": "r_p", "parent_table": "P",
             "parent_row_id": "P2"}])
        p1 = self.explain(out, "P", "P1")
        self.assertEqual(p1["status"], LOCAL_REJECT)

    def test_both_candidates_fail_proof_lists_both_and_layers(self):
        # P1 locally rejected (non-nullable 'broken' is null); P2 is
        # locally valid but its mandatory r_gone reference points at a key
        # nobody carries -> P2 layer 1, D layer 2; D's proof names both.
        write_jsonl(self.data / "p.jsonl", [
            wrap("P1", {"id": "P1", "pk": "K1", "broken": None},
                 source_file="p.jsonl", line_number=1),
            wrap("P2", {"id": "P2", "pk": "K1", "broken": "ok",
                        "gone": "ghost"},
                 source_file="p.jsonl", line_number=2)])
        write_jsonl(self.data / "d.jsonl",
                    [wrap("D1", {"did": "D1", "fk": "K1"},
                          source_file="d.jsonl", rid_field="did")])
        write_jsonl(self.data / "t.jsonl", [])
        contract = [
            table("P", [self._p("p.jsonl")],
                  {"id": field(["id"]), "pk": field(["pk"]),
                   "broken": field(["broken"]),
                   "gone": field(["gone"], nullable=True)},
                  candidate_keys={"kp": ["pk"]},
                  references=[ref("r_gone", ["gone"], "T", "kt",
                                  "mandatory")]),
            table("T", [self._p("t.jsonl")],
                  {"id": field(["id"])},
                  candidate_keys={"kt": ["id"]}),
            table("D", [self._p("d.jsonl")],
                  {"did": field(["did"]), "fk": field(["fk"])},
                  row_id_path=["did"], candidate_keys={"kd": ["did"]},
                  references=[ref("r_p", ["fk"], "P", "kp")])]
        out, _ = self.run_job(contract)
        p1 = self.explain(out, "P", "P1")
        p2 = self.explain(out, "P", "P2")
        d = self.explain(out, "D", "D1")
        self.assertEqual((p1["status"], p1["layer"]), (LOCAL_REJECT, 0))
        self.assertEqual((p2["status"], p2["layer"]), (CASCADE_REJECT, 1))
        self.assertEqual((d["status"], d["layer"]), (CASCADE_REJECT, 2))
        cands = d["primary_proof"]["initial_candidates"]
        self.assertEqual([c["parent_row_id"] for c in cands],
                         ["P1", "P2"])
        self.assertEqual([c["parent_layer"] for c in cands], [0, 1])
        self.assertIsNone(cands[0]["parent_proof_id"])  # local row
        self.assertIsNotNone(cands[1]["parent_proof_id"])


class TestStablePrimarySelection(AdmissionCase):
    """Two failing references at the same layer: byte-ordered name wins."""

    def test_utf8_byte_order_tie_break(self):
        write_jsonl(self.data / "c.jsonl", [
            wrap("C1", {"id": "C1", "a": "x", "b": "y"},
                 source_file="c.jsonl")])
        write_jsonl(self.data / "empty.jsonl", [])
        contract = [
            table("C", [self._p("c.jsonl")],
                  {"id": field(["id"]), "a": field(["a"]),
                   "b": field(["b"])},
                  candidate_keys={"kc": ["id"]},
                  references=[ref("zzz", ["a"], "E", "ke"),
                              ref("aaa", ["b"], "E", "ke")]),
            table("E", [self._p("empty.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]})]
        out, _ = self.run_job(contract)
        c = self.explain(out, "C", "C1")
        self.assertEqual(c["layer"], 1)
        self.assertEqual(c["primary_proof"]["reference"], "aaa")
        names = {f["reference"] for f in c["all_failing_references"]}
        self.assertEqual(names, {"aaa", "zzz"})
        # Non-ASCII names follow UTF-8 byte order, not Python str order.
        write_jsonl(self.data / "c2.jsonl", [
            wrap("C2", {"id": "C2", "a": "x", "b": "y"},
                 source_file="c2.jsonl")])
        contract2 = [
            table("C2", [self._p("c2.jsonl")],
                  {"id": field(["id"]), "a": field(["a"]),
                   "b": field(["b"])},
                  candidate_keys={"kc2": ["id"]},
                  references=[ref("é", ["a"], "E", "ke"),
                              ref("z", ["b"], "E", "ke")]),
            table("E", [self._p("empty.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]})]
        out2, _ = self.run_job(contract2)
        c2 = self.explain(out2, "C2", "C2")
        # "é" encodes to C3 A9, which sorts after ASCII "z" (7A).
        self.assertEqual(c2["primary_proof"]["reference"], "z")


class TestDiamondSharing(AdmissionCase):
    """One root failure fans out through two parents into one child.

    R(oot) -> empty; M1 -> R, M2 -> R; D -> M1 and D -> M2 via two
    references sharing the same root key.  The root's unsupported group is
    one proof node reused by both middle rows; D's two proofs share it.
    """

    def test_one_proof_node_for_shared_root_cause(self):
        write_jsonl(self.data / "r.jsonl", [
            wrap("R1", {"id": "R1", "gone": "nope"}, source_file="r.jsonl")])
        write_jsonl(self.data / "m.jsonl", [
            wrap("M1", {"id": "M1", "fk": "R1"}, source_file="m.jsonl",
                 line_number=1),
            wrap("M2", {"id": "M2", "fk": "R1"}, source_file="m.jsonl",
                 line_number=2)])
        write_jsonl(self.data / "d.jsonl", [
            wrap("D1", {"id": "D1", "f1": "M1", "f2": "M2"},
                 source_file="d.jsonl")])
        write_jsonl(self.data / "e.jsonl", [])
        contract = [
            table("E", [self._p("e.jsonl")],
                  {"id": field(["id"])}, candidate_keys={"ke": ["id"]}),
            table("R", [self._p("r.jsonl")],
                  {"id": field(["id"]), "gone": field(["gone"])},
                  candidate_keys={"kr": ["id"]},
                  references=[ref("r_gone", ["gone"], "E", "ke")]),
            table("M", [self._p("m.jsonl")],
                  {"id": field(["id"]), "fk": field(["fk"])},
                  candidate_keys={"km": ["id"]},
                  references=[ref("r_r", ["fk"], "R", "kr")]),
            table("D", [self._p("d.jsonl")],
                  {"id": field(["id"]), "f1": field(["f1"]),
                   "f2": field(["f2"])},
                  candidate_keys={"kd": ["id"]},
                  references=[ref("r_m1", ["f1"], "M", "km"),
                              ref("r_m2", ["f2"], "M", "km")])]
        out, report = self.run_job(contract)
        db = sqlite3.connect(
            f"file:{out / 'admission.sqlite3'}?mode=ro", uri=True)
        # The shared root group is stored exactly once.
        root_groups = db.execute(
            "SELECT COUNT(*) FROM edge_groups g JOIN table_ordinals t "
            "ON t.ordinal = g.table_ord WHERE g.ref_name = 'r_gone'"
        ).fetchone()[0]
        self.assertEqual(root_groups, 1)
        node_count = db.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
        edge_count = db.execute(
            "SELECT COUNT(*) FROM proof_candidates").fetchone()[0]
        d = self.explain(out, "D", "D1")
        self.assertEqual(d["layer"], 3)
        proof_ids = {f["proof_id"] for f in d["all_failing_references"]}
        self.assertEqual(len(proof_ids), 2)
        # Both M rows' proofs must reference the very same root node.
        root_proof_ids = set()
        for f in d["all_failing_references"]:
            node = get_proof_node(out, f["proof_id"])
            for cand in node["initial_candidates"]:
                if cand["parent_row_id"] in ("M1", "M2"):
                    root_proof_ids.add(cand["parent_proof_id"])
        self.assertEqual(len(root_proof_ids), 1)
        # Four rejected rows (R, M1, M2, D) -> four proof nodes; only the
        # three nodes with non-empty candidate sets carry edges (one each),
        # so edges stay linear instead of expanding per dependent path.
        self.assertEqual(node_count, 4)
        self.assertEqual(edge_count, 3)
        # And no candidate list is ever duplicated: the root node is the
        # unique owner of the empty-candidate r_gone fact.
        empty_nodes = db.execute(
            "SELECT COUNT(*) FROM proofs p WHERE NOT EXISTS ("
            "SELECT 1 FROM proof_candidates c WHERE c.proof_id = p.proof_id)"
        ).fetchone()[0]
        self.assertEqual(empty_nodes, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
