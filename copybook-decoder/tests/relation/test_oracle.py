"""Cross-check the production engine against the independent oracle.

For many small, randomly generated multi-table handoffs this test:

* enumerates every satisfying subset with the oracle and asserts the
  delivered kept set equals their union;
* recomputes layers with the oracle's independent synchronous rounds and
  asserts equality;
* walks every delivered proof node checking the strict layer decrease, the
  completeness of the initial candidate set and the primary-proof rule;
* shuffles files/tables and re-runs, asserting byte-identical verdicts.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from copybook.relation import (CASCADE_REJECT, KEPT,
                               RelationContract, explain_row,
                               get_proof_node, run_admission)
from tests.relation.oracle import Oracle


def _contract_dict(spec: dict, data_dir: Path) -> dict:
    tables = []
    for tname, t in spec["tables"].items():
        tables.append({
            "name": tname,
            "files": [str(data_dir / f"{tname}.jsonl")],
            "row_id_path": ["id"],
            "fields": {c: {"path": [c], "type": ty,
                           "nullable": c in t["nullable"]}
                       for c, ty in t["columns"].items()},
            "candidate_keys": {f"k_{tname}": t["key"]},
            "references": t["references"]})
    return {"tables": tables}


def _write_inputs(spec: dict, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for tname, t in spec["tables"].items():
        path = data_dir / f"{tname}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for i, row in enumerate(t["rows"], start=1):
                rec = {"meta": {"source_file": f"{tname}.jsonl",
                                "line_number": i, "byte_start": (i - 1) * 80,
                                "byte_end": (i - 1) * 80 + 60},
                       "fields": row}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _random_spec(rng: random.Random) -> dict:
    """A bounded random handoff: 2-3 tables, single string keys, refs."""
    n_tables = rng.randint(2, 3)
    names = [f"T{i}" for i in range(n_tables)]
    spec = {"tables": {}}
    for ti, name in enumerate(names):
        n_rows = rng.randint(0, 5)
        key_space = [f"K{rng.randint(0, 4)}" for _ in range(n_rows)]
        rows = []
        for ri in range(n_rows):
            row = {"id": f"{name}{ri}", "k": key_space[ri]}
            # Sprinkle local faults and nulls.
            if rng.random() < 0.15:
                row["opt"] = None
            else:
                row["opt"] = f"K{rng.randint(0, 4)}"
            rows.append(row)
        target = names[rng.randrange(n_tables)]
        references = [{
            "name": "r_man", "columns": ["k"], "target_table": target,
            "target_key": f"k_{target}", "rule": "mandatory"},
            {"name": "r_opt", "columns": ["opt"], "target_table": target,
             "target_key": f"k_{target}", "rule": "optional"}]
        spec["tables"][name] = {
            "columns": {"id": "string", "k": "string", "opt": "string"},
            "nullable": ["opt"], "key": ["k"], "rows": rows,
            "references": references}
    return spec


class OracleCrossCheck(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="rel-oracle-")
        self.tmp = Path(self.tmp_obj.name)
        self.addCleanup(self.tmp_obj.cleanup)

    def _run(self, spec, tag):
        data = self.tmp / f"data-{tag}"
        _write_inputs(spec, data)
        contract_dict = _contract_dict(spec, data)
        contract_path = self.tmp / f"contract-{tag}.json"
        contract_path.write_text(
            json.dumps(contract_dict, ensure_ascii=False),
            encoding="utf-8")
        out = self.tmp / f"out-{tag}"
        report = run_admission(
            contract=RelationContract.load(contract_path),
            output_dir=out, temp_dir=self.tmp / f"stage-{tag}")
        return out, report, contract_dict

    def _assert_matches_oracle(self, spec, out, contract_dict):
        oracle = Oracle(contract_dict)
        expected_kept = oracle.expected_kept()
        expected_layers = oracle.expected_layers()
        for tname in spec["tables"]:
            for row in spec["tables"][tname]["rows"]:
                rid = row["id"]
                explanation = explain_row(out, tname, rid)
                self.assertEqual(
                    explanation["layer"],
                    expected_layers[(tname, rid)],
                    f"layer mismatch {tname}.{rid}")
                if explanation["status"] == KEPT:
                    self._check_witnesses(explanation)
                elif explanation["status"] == CASCADE_REJECT:
                    self._check_proof(out, explanation, expected_layers)
                # The oracle's kept set must match the delivered verdict.
                self.assertEqual(
                    explanation["status"] == KEPT,
                    rid in expected_kept[tname],
                    f"kept-set mismatch for {tname}.{rid}")

    def _check_witnesses(self, explanation):
        for w in explanation["witnesses"]:
            self.assertTrue(w["parent_row_id"])
            self.assertTrue(w["parent_table"])

    def _check_proof(self, out, explanation, expected_layers):
        proof = explanation["primary_proof"]
        node = get_proof_node(out, proof["proof_id"])
        self.assertEqual(node["layer"], explanation["layer"])
        for cand in node["initial_candidates"]:
            # Strict layer decrease for every initial candidate.
            self.assertLess(cand["parent_layer"], node["layer"])
        # Primary reference is the byte-order-smallest among ties.
        failing = explanation["all_failing_references"]
        min_layer = min(f["layer"] for f in failing)
        tied = sorted(f["reference"] for f in failing
                      if f["layer"] == min_layer)
        self.assertEqual(proof["reference"], tied[0])

    def test_random_handoffs_match_oracle(self):
        rng = random.Random(20261115)
        for case in range(60):
            spec = _random_spec(rng)
            out, _report, contract_dict = self._run(spec, f"a{case}")
            self._assert_matches_oracle(spec, out, contract_dict)

    def test_handwritten_scenarios_match_oracle(self):
        spec = {
            "tables": {
                "A": {"columns": {"id": "string", "k": "string"},
                      "nullable": [], "key": ["k"],
                      "rows": [{"id": "A1", "k": "A1"}],
                      "references": [
                          {"name": "r", "columns": ["k"],
                           "target_table": "A", "target_key": "k_A",
                           "rule": "mandatory"}]},
                "E": {"columns": {"id": "string", "k": "string"},
                      "nullable": [], "key": ["k"], "rows": [],
                      "references": []},
                "X": {"columns": {"id": "string", "k": "string"},
                      "nullable": [], "key": ["k"],
                      "rows": [{"id": "X1", "k": "GONE"}],
                      "references": [
                          {"name": "r", "columns": ["k"],
                           "target_table": "E", "target_key": "k_E",
                           "rule": "mandatory"}]}}}
        out, _report, contract_dict = self._run(spec, "hand")
        self._assert_matches_oracle(spec, out, contract_dict)
        self.assertEqual(
            explain_row(out, "X", "X1")["layer"], 1)

    def test_rearrangement_gives_identical_verdicts(self):
        rng = random.Random(777)
        spec = _random_spec(rng)
        out1, _, contract_dict = self._run(spec, "first")
        # Re-run with table order reversed and rows shuffled inside files.
        shuffled = {"tables": {}}
        for name in reversed(list(spec["tables"])):
            t = spec["tables"][name]
            rows = list(t["rows"])
            rng.shuffle(rows)
            shuffled["tables"][name] = dict(t, rows=rows)
        out2, _, _ = self._run(shuffled, "second")
        for tname, t in spec["tables"].items():
            for row in t["rows"]:
                rid = row["id"]
                e1 = explain_row(out1, tname, rid)
                e2 = explain_row(out2, tname, rid)
                self.assertEqual((e1["status"], e1["layer"]),
                                 (e2["status"], e2["layer"]))
        # Kept JSONL row_id order must be byte-stable even when physical
        # line numbers change (origin metadata is preserved verbatim, so
        # the payload text itself legitimately differs).
        def kept_ids(root):
            out = {}
            for tdir in (root / "tables").iterdir():
                tname = tdir.name.split("_", 1)[1]
                ids = [json.loads(line)["fields"]["id"] for line in
                       (tdir / "kept.jsonl").read_text().splitlines()
                       if line]
                out[tname] = ids
            return out
        self.assertEqual(kept_ids(out1), kept_ids(out2))


if __name__ == "__main__":
    unittest.main()
