#!/usr/bin/env python3
"""Scale driver: a long chain cascade and one huge same-key candidate group.

Two structures are built and run through the real admission engine:

1. **chain** -- N rows where row i references row i-1 and the root
   references a key nobody carries.  The whole chain must fail in N rounds;
   this proves the synchronous layer propagation and that no full graph is
   ever materialized in Python.
2. **big group** -- M parent rows all carrying the same candidate key plus
   many children referencing that key.  Only one shared proof node exists
   for the key; its complete candidate list stays inside SQLite and is
   streamed, never loaded as one giant Python container.

The driver reports wall time, peak RSS, the staging database footprint,
the delivered proof node/edge counts and, for the big group, the
single-node sharing that keeps the output linear.

Run:  python3 examples/relation/run_performance.py [chain_n] [group_m]
"""

from __future__ import annotations

import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.relation import RelationContract, run_admission


def _rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _record(fields, source_file, line_number):
    start = (line_number - 1) * 80
    return {"meta": {"source_file": source_file, "line_number": line_number,
                     "byte_start": start, "byte_end": start + 60},
            "fields": fields}


def build_chain(root: Path, n: int) -> Path:
    data = root / "chain"
    data.mkdir(parents=True)
    chain_file = data / "chain.jsonl"
    with chain_file.open("w", encoding="utf-8") as fh:
        # Root points at a missing key; every later row points one step up.
        fh.write(json.dumps(_record(
            {"id": "C0", "up": "MISSING-ROOT"}, "chain.jsonl", 1)) + "\n")
        for i in range(1, n):
            fh.write(json.dumps(_record(
                {"id": f"C{i}", "up": f"C{i - 1}"},
                "chain.jsonl", i + 1)) + "\n")
    empty = data / "sink.jsonl"
    empty.write_text("", encoding="utf-8")
    contract = {
        "tables": [
            {"name": "chain", "files": [str(chain_file)],
             "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"},
                        "up": {"path": ["up"], "type": "string"}},
             "candidate_keys": {"kc": ["id"]},
             "references": [{"name": "r_up", "columns": ["up"],
                             "target_table": "sink",
                             "target_key": "ks", "rule": "mandatory"}]},
            {"name": "sink", "files": [str(empty)],
             "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"}},
             "candidate_keys": {"ks": ["id"]}}]}
    # The chain must reference itself for the cascade; rewrite to point at
    # the chain's own key while the root's missing value still breaks it.
    contract["tables"][0]["references"][0]["target_table"] = "chain"
    contract["tables"][0]["references"][0]["target_key"] = "kc"
    cp = root / "chain_contract.json"
    cp.write_text(json.dumps(contract), encoding="utf-8")
    return cp


def build_big_group(root: Path, m: int, children: int) -> Path:
    data = root / "group"
    data.mkdir(parents=True)
    parents = data / "parents.jsonl"
    # All M parents carry the same business key "SHARED-KEY" but have
    # distinct row ids; each also has a mandatory reference to a missing
    # key, so every parent is eventually rejected and the children cascade.
    with parents.open("w", encoding="utf-8") as fh:
        for i in range(m):
            fh.write(json.dumps(_record(
                {"id": f"P{i:08d}", "shared": "SHARED-KEY",
                 "gone": "NO-SUCH"}, "parents.jsonl", i + 1)) + "\n")
    child_file = data / "children.jsonl"
    with child_file.open("w", encoding="utf-8") as fh:
        for j in range(children):
            fh.write(json.dumps(_record(
                {"id": f"D{j:08d}", "fk": "SHARED-KEY"},
                "children.jsonl", j + 1)) + "\n")
    empty = data / "sink.jsonl"
    empty.write_text("", encoding="utf-8")
    contract = {
        "tables": [
            {"name": "sink", "files": [str(empty)],
             "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"}},
             "candidate_keys": {"ks": ["id"]}},
            {"name": "parents", "files": [str(parents)],
             "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"},
                        "shared": {"path": ["shared"], "type": "string"},
                        "gone": {"path": ["gone"], "type": "string"}},
             "candidate_keys": {"kp": ["shared"]},
             "references": [{"name": "r_gone", "columns": ["gone"],
                             "target_table": "sink",
                             "target_key": "ks", "rule": "mandatory"}]},
            {"name": "children", "files": [str(child_file)],
             "row_id_path": ["id"],
             "fields": {"id": {"path": ["id"], "type": "string"},
                        "fk": {"path": ["fk"], "type": "string"}},
             "candidate_keys": {"kd": ["id"]},
             "references": [{"name": "r_p", "columns": ["fk"],
                             "target_table": "parents",
                             "target_key": "kp", "rule": "mandatory"}]}]}
    cp = root / "group_contract.json"
    cp.write_text(json.dumps(contract), encoding="utf-8")
    return cp


def measure(label: str, contract_path: Path, work: Path) -> None:
    out = work / f"{label}-out"
    stage = work / f"{label}-stage"
    start = time.monotonic()
    report = run_admission(
        contract=RelationContract.load(contract_path),
        output_dir=out, temp_dir=stage)
    elapsed = time.monotonic() - start
    staging_bytes = sum(p.stat().st_size for p in out.rglob("*")
                        if p.is_file())
    print(f"\n[{label}]")
    print(f"  wall time            : {elapsed:.2f}s")
    print(f"  peak RSS (KB)        : {_rss_kb()}")
    print(f"  delivery bytes       : {staging_bytes:,}")
    print(f"  identities           : {report['identity_count']}")
    print(f"  elimination rounds   : {report['elimination_rounds']}")
    print(f"  proof nodes / edges  : {report['proof_node_count']} / "
          f"{report['proof_edge_count']}")
    print(f"  witnesses            : {report['witness_count']}")
    for table, counts in report["counts"].items():
        print(f"  {table:>9}: {counts}")
    print(f"  conclusion           : {report['conclusion']}")


def main() -> None:
    chain_n = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
    group_m = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    children = max(1000, group_m // 10)
    work = Path(tempfile.mkdtemp(prefix="relation-perf-"))
    try:
        chain_contract = build_chain(work, chain_n)
        measure(f"chain-n{chain_n}", chain_contract, work)
        group_contract = build_big_group(work, group_m, children)
        measure(f"group-m{group_m}-d{children}", group_contract, work)
        # Sharing assertion for the big group: exactly one children proof
        # node, and its candidate list has all M parents but the node row
        # count does not grow with the number of children.
        import sqlite3
        db = sqlite3.connect(
            f"file:{work / f'group-m{group_m}-d{children}-out' / 'admission.sqlite3'}?mode=ro",
            uri=True)
        child_nodes = db.execute(
            "SELECT COUNT(*) FROM proofs p JOIN edge_groups g "
            "ON g.group_id = p.group_id WHERE g.ref_name = 'r_p'"
        ).fetchone()[0]
        cand = db.execute(
            "SELECT COUNT(*) FROM proof_candidates pc JOIN proofs p "
            "ON p.proof_id = pc.proof_id JOIN edge_groups g "
            "ON g.group_id = p.group_id WHERE g.ref_name = 'r_p'"
        ).fetchone()[0]
        db.close()
        assert child_nodes == 1, child_nodes
        assert cand == group_m, (cand, group_m)
        print(f"\nbig-group sharing verified: 1 proof node carrying "
              f"{cand} candidates for {children} children")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
