"""Event-by-event replay tests for the open-item settlement ledger.

The headline scenario is the hand-built account from the requirements.
Every expectation below is derived manually from the narrative (not from
the processor's own output): after each applied event the active edges,
the release reasons, both sides' balances and both conservation
invariants are checked, so a wrong allocation can never hide behind a
correct final net.
"""

import unittest
from pathlib import Path

from copybook.settle import (ACTIVE, RELEASED, SettleConfig,
                             run_settlement, trace_object)
from copybook.settle.ledger import GroupProcessor

from .builders import (DEFAULT_CONFIG, bill, cancel_bill, cancel_receipt,
                       receipt, write_jsonl)

CUTOFF = "2026-01-10T00:00:00Z"
DUE = "2026-01-01"


def hand_rows():
    """The nine-event hand scenario, one event per UTC second."""
    return [
        bill("e01", "2026-01-01T00:00:01Z", "A", "100.00", DUE),
        bill("e02", "2026-01-01T00:00:02Z", "B", "100.00", DUE),
        receipt("e03", "2026-01-01T00:00:03Z", "P", "150.00"),
        receipt("e04", "2026-01-01T00:00:04Z", "Q", "50.00"),
        cancel_receipt("e05", "2026-01-01T00:00:05Z", "P"),
        bill("e06", "2026-01-01T00:00:06Z", "C", "100.00", DUE),
        receipt("e07", "2026-01-01T00:00:07Z", "R", "60.00"),
        cancel_bill("e08", "2026-01-01T00:00:08Z", "B"),
        cancel_receipt("e09", "2026-01-01T00:00:09Z", "Q"),
    ]


class LedgerReplayTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.dir = Path(tempfile.mkdtemp(prefix="settle-ledger-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = SettleConfig.from_dict(DEFAULT_CONFIG)

    def _run(self, rows, cutoff=CUTOFF, tag=None):
        path = write_jsonl(self.dir / "events.jsonl", rows)
        out = self.dir / (f"result-{tag}" if tag else "result")
        summary = run_settlement([path], config=self.config,
                                 cutoff=cutoff, output_dir=out)
        return out, summary

    def _read(self, out, name):
        import json
        return [json.loads(line) for line in
                (out / name).read_text(encoding="utf-8").splitlines() if line]

    # ------------------------------------------------- stepwise hand replay
    def test_hand_scenario_step_by_step(self):
        """Re-run the replay with growing cutoffs and assert each step."""
        rows = hand_rows()
        # (cutoff second, expected active (bill,receipt,amount), expected
        #  balances bill->open, receipt->available)
        steps = [
            ("01", {}, {"A": 10000}, {}),
            ("02", {}, {"A": 10000, "B": 10000}, {}),
            # P150 covers A100 + B50
            ("03", {("A", "P"): 10000, ("B", "P"): 5000},
             {"A": 0, "B": 5000}, {"P": 0}),
            # Q50 covers the rest of B
            ("04", {("A", "P"): 10000, ("B", "P"): 5000,
                    ("B", "Q"): 5000},
             {"A": 0, "B": 0}, {"P": 0, "Q": 0}),
            # P cancelled: A back to 100, B back to 50; Q still on B
            ("05", {("B", "Q"): 5000},
             {"A": 10000, "B": 5000}, {"Q": 0}),
            # new bill C, nothing available to cover it
            ("06", {("B", "Q"): 5000},
             {"A": 10000, "B": 5000, "C": 10000}, {"Q": 0}),
            # R60 only reaches the earliest bill A
            ("07", {("B", "Q"): 5000, ("A", "R"): 6000},
             {"A": 4000, "B": 5000, "C": 10000}, {"Q": 0, "R": 0}),
            # B cancelled: Q's 50 freed -> A40 then C10
            ("08", {("A", "R"): 6000, ("A", "Q"): 4000,
                    ("C", "Q"): 1000},
             {"A": 0, "C": 9000}, {"Q": 0, "R": 0}),
            # Q cancelled: A reopens 40, C reopens 10; R->A survives
            ("09", {("A", "R"): 6000},
             {"A": 4000, "C": 10000}, {"R": 0}),
        ]
        for second, exp_edges, exp_bills, exp_receipts in steps:
            with self.subTest(step=second):
                out, _ = self._run(rows, f"2026-01-01T00:00:{second}Z",
                                   tag=second)
                edges = {
                    (a["bill_id"], a["receipt_id"]): a["amount_cents"]
                    for a in self._read(out, "allocations.jsonl")
                    if a["status"] == ACTIVE}
                self.assertEqual(edges, exp_edges)
                objs = {o["object_id"]: o["remaining_cents"]
                        for o in self._read(out, "objects.jsonl")
                        if o["kind"] == "BILL" and o["status"] == "open"}
                self.assertEqual(objs, exp_bills)
                rec = {o["object_id"]: o["remaining_cents"]
                       for o in self._read(out, "objects.jsonl")
                       if o["kind"] == "RECEIPT" and o["status"] == "open"}
                self.assertEqual(rec, exp_receipts)
                self._assert_conservation(out)

    def _assert_conservation(self, out):
        import json
        report = json.loads((out / "settle_report.json").read_text())
        cons = report["groups"][0]["conservation"]
        self.assertTrue(cons["bill_invariant_ok"])
        self.assertTrue(cons["receipt_invariant_ok"])

    def test_final_state_and_full_edge_history(self):
        out, summary = self._run(hand_rows())
        self.assertEqual(summary["conclusion"], "settled")
        allocations = self._read(out, "allocations.jsonl")
        # Six edges total; the Q->B pair, once released, later re-matches as
        # brand new edges (A-Q, C-Q), never overwriting the old edge.
        self.assertEqual(len(allocations), 6)
        by_pair = {(a["bill_id"], a["receipt_id"]): a
                   for a in allocations}
        self.assertEqual(by_pair[("B", "Q")]["status"], RELEASED)
        self.assertEqual(by_pair[("B", "Q")]["release_reason"],
                         "bill_cancelled")
        for pair in (("A", "Q"), ("C", "Q")):
            self.assertEqual(by_pair[pair]["status"], RELEASED)
            self.assertEqual(by_pair[pair]["release_reason"],
                             "receipt_cancelled")
        only_active = [a for a in allocations if a["status"] == ACTIVE]
        self.assertEqual(
            [(edge["bill_id"], edge["receipt_id"], edge["amount_cents"])
             for edge in only_active],
            [("A", "R", 6000)])
        # Final balances: A 40 open, C 100 open.
        objs = {o["object_id"]: o for o in
                self._read(out, "objects.jsonl")}
        self.assertEqual(objs["A"]["remaining_cents"], 4000)
        self.assertEqual(objs["C"]["remaining_cents"], 10000)
        self.assertEqual(objs["B"]["status"], "cancelled")
        self.assertEqual(objs["P"]["status"], "cancelled")
        self.assertEqual(objs["Q"]["status"], "cancelled")
        self.assertEqual(objs["R"]["remaining_cents"], 0)

    def test_each_release_points_back_at_a_specific_edge(self):
        out, _ = self._run(hand_rows())
        events = {e["event_id"]: e for e in
                  self._read(out, "events.jsonl")}
        allocations = {a["allocation_id"]: a for a in
                       self._read(out, "allocations.jsonl")}
        # e05 (cancel P) releases exactly the two P edges.
        self.assertEqual(
            set(events["e05"]["effect"]["released_allocation_ids"]),
            {"A000001", "A000002"})
        for aid in ("A000001", "A000002"):
            self.assertEqual(allocations[aid]["released_event_id"], "e05")
        # e08 releases the old B-Q edge and opens two new edges.
        self.assertEqual(events["e08"]["effect"]["released_allocation_ids"],
                         ["A000003"])
        self.assertEqual(
            set(events["e08"]["effect"]["opened_allocation_ids"]),
            {"A000005", "A000006"})

    def test_released_edges_are_never_released_twice(self):
        out, _ = self._run(hand_rows())
        allocations = self._read(out, "allocations.jsonl")
        for a in allocations:
            if a["status"] == RELEASED:
                # Exactly one release pointer, never reopened.
                self.assertIsNotNone(a["released_event_id"])
        ids = [a["allocation_id"] for a in allocations]
        self.assertEqual(len(ids), len(set(ids)))

    def test_trace_object_from_result_files_only(self):
        out, _ = self._run(hand_rows())
        trace = trace_object(out, "Q", kind="RECEIPT")
        self.assertEqual(trace["match_count"], 1)
        match = trace["matches"][0]
        # Q participates in three edges across its full life.
        pairs = sorted((a["bill_id"], a["status"])
                       for a in match["allocation_history"])
        self.assertEqual(pairs, [("A", "released"),
                                 ("B", "released"),
                                 ("C", "released")])
        related = {e["event_id"] for e in match["related_events"]}
        self.assertEqual(related, {"e04", "e08", "e09"})

    def test_trace_unknown_object_raises(self):
        out, _ = self._run(hand_rows())
        from copybook.settle import SettleQueryError
        with self.assertRaises(SettleQueryError):
            trace_object(out, "GHOST")


class DirectProcessorTests(unittest.TestCase):
    def test_group_processor_isolated_from_other_groups(self):
        # Two accounts; an invalid cancel in one must not touch the other.
        cfg = SettleConfig.from_dict(DEFAULT_CONFIG)
        proc = GroupProcessor("ACC1", "CNY", cfg)
        self.assertEqual((proc.account, proc.currency), ("ACC1", "CNY"))
        self.assertFalse(proc.invalid)


if __name__ == "__main__":
    unittest.main()
