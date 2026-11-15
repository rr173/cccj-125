#!/usr/bin/env python3
"""Run the relation admission scenario and verify every hand-written fact.

Expected outcome (derived by hand before running the engine)::

    branches BR1, BR2          kept (BR1 self-root, BR2 -> BR1)
    branches BR3               cascade reject layer 1 (r_parent unmet)
    accounts 0000000001/2      kept; their r_gone column is null and the
                               reference is mandatory, so it simply has no
                               match -> they would fail; instead the
                               scenario sets GONE-KEY only on account 3.
    accounts 0000000003        cascade reject layer 1 (r_gone -> key GHOST
                               absent from the terminated table)
    txn T001, T004             kept (T004's optional r_branch is all-null
                               and explicitly exempt)
    txn T002                   cascade reject layer 1 (no account 99)
    txn T003                   cascade reject layer 2 (its account 3 is a
                               layer-1 reject; branch BR3 also fails)

The script rebuilds inputs, runs the job through the public Python API and
then reopens the delivery directory in a fresh read-only connection to
check the proofs, witnesses, counts, ordering and completion marker.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.relation import (CASCADE_REJECT, KEPT, LOCAL_REJECT,
                               RelationContract, explain_row,
                               get_proof_node, iter_kept_rows,
                               run_admission)

from build_scenarios import WORK, main as build_main

OUT = HERE / "run" / "delivery"
STAGE = HERE / "run" / "stage"

EXPECTED = {
    "branches": {"BR1": (KEPT, None), "BR2": (KEPT, None),
                 "BR3": (CASCADE_REJECT, 1)},
    "accounts": {"0000000001": (KEPT, None),
                 "0000000002": (KEPT, None),
                 "0000000003": (CASCADE_REJECT, 1)},
    "transactions": {"T001": (KEPT, None), "T002": (CASCADE_REJECT, 1),
                     "T003": (CASCADE_REJECT, 2), "T004": (KEPT, None)},
}


def check() -> None:
    build_main()
    if OUT.exists():
        shutil.rmtree(OUT)
    if STAGE.exists():
        shutil.rmtree(STAGE)
    contract = RelationContract.load(HERE / "run" / "contract.json")
    report = run_admission(contract=contract, output_dir=OUT,
                           temp_dir=STAGE)
    assert (OUT / "COMPLETE.json").is_file(), "completion marker missing"

    for table, rows in EXPECTED.items():
        for row_id, (status, layer) in rows.items():
            explanation = explain_row(OUT, table, row_id)
            assert explanation["status"] == status, (table, row_id,
                                                     explanation["status"])
            assert explanation["layer"] == layer, (table, row_id,
                                                   explanation["layer"])
            print(f"  {table}.{row_id:>10}: {status} layer={layer}")

    # T003's proof must name account 0000000003 as its initial candidate,
    # and that candidate's own proof node must be reachable.
    t003 = explain_row(OUT, "transactions", "T003")
    proof = t003["primary_proof"]
    assert proof["reference"] == "r_acct"
    cand_ids = [c["parent_row_id"]
                for c in proof["initial_candidates"]]
    assert cand_ids == ["0000000003"], cand_ids
    parent_proof_id = proof["initial_candidates"][0]["parent_proof_id"]
    parent_node = get_proof_node(OUT, parent_proof_id)
    assert parent_node["reference"] == "r_gone"
    # No row carries the GHOST key, so the initial candidate set is empty.
    assert parent_node["initial_candidates"] == []
    print("  T003 chain proof: r_acct -> account 3 -> r_gone (empty set) OK")

    # T004 carries an explicit all-null optional marker and no r_branch
    # witness, but still has its mandatory account witness.
    t004 = explain_row(OUT, "transactions", "T004")
    assert t004["all_null_optional_references"] == ["r_branch"]
    assert [w["reference"] for w in t004["witnesses"]] == ["r_acct"]
    print("  T004 all-null optional exemption OK")

    # Counters reconcile per table.
    for table, counts in report["counts"].items():
        assert counts["input"] == (counts["kept"]
                                   + counts["local_reject"]
                                   + counts["cascade_reject"]), table
    print("  per-table counts reconcile:",
          json.dumps(report["counts"], ensure_ascii=False))

    # Kept rows stream back in UTF-8 byte order using only the delivery.
    kept = [r["fields"][list(r["fields"])[0]] for r in
            iter_kept_rows(OUT, "transactions")]
    assert kept == sorted(kept, key=lambda s: s.encode("utf-8")), kept
    print("  admitted transactions in byte order:", kept)

    # The delivered database is read-only reopenable and self-contained.
    db = sqlite3.connect(f"file:{OUT / 'admission.sqlite3'}?mode=ro",
                         uri=True)
    identities = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    total_input = sum(c["input"] for c in report["counts"].values())
    assert identities == total_input
    db.close()
    print(f"  self-contained delivery: {identities} identities, "
          f"{report['proof_node_count']} proof nodes, "
          f"{report['proof_edge_count']} proof edges, "
          f"{report['witness_count']} witnesses")
    print("\nALL SCENARIO CHECKS PASSED")


if __name__ == "__main__":
    check()
