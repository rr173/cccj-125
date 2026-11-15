"""Additional structural edge cases for the settlement ledger."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.settle import ACTIVE, RELEASED, SettleConfig, run_settlement

from .builders import (DEFAULT_CONFIG, bill, cancel_receipt,
                       receipt, write_jsonl)


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="settle-struct-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = SettleConfig.from_dict(DEFAULT_CONFIG)

    def settle(self, rows, cutoff="2026-06-01T00:00:00Z", tag="r"):
        path = write_jsonl(self.dir / f"{tag}.jsonl", rows)
        out = self.dir / f"out-{tag}"
        return out, run_settlement([path], config=self.config,
                                   cutoff=cutoff, output_dir=out)

    def lines(self, out, name):
        p = out / name
        return [json.loads(x) for x in
                p.read_text(encoding="utf-8").splitlines() if x]

    def test_optional_routes_may_be_absent_per_kind(self):
        # Bills carry no target_id; receipts carry no due_on. The configured
        # paths simply resolve to nothing for the kinds that do not use them.
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "B", "10.00", "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "10.00"),
        ]
        out, summary = self.settle(rows, tag="optional")
        self.assertEqual(summary["conclusion"], "settled")
        routed = {e["event_id"]: e["routed"]
                  for e in self.lines(out, "events.jsonl")}
        self.assertIsNone(routed["b1"]["target_id"])
        self.assertIsNone(routed["r1"]["due_on"])

    def test_one_receipt_covers_many_bills_and_one_bill_many_receipts(self):
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "B1", "100.00",
                 "2026-02-01"),
            bill("b2", "2026-01-01T00:00:02Z", "B2", "100.00",
                 "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:03Z", "R1", "150.00"),
            receipt("r2", "2026-01-01T00:00:04Z", "R2", "60.00"),
        ]
        out, _ = self.settle(rows, tag="fanout")
        edges = {(a["bill_id"], a["receipt_id"]): a["amount_cents"]
                 for a in self.lines(out, "allocations.jsonl")
                 if a["status"] == ACTIVE}
        # R1: B1 100 then B2 50; R2 finishes B2's remaining 50, 10 unallocated.
        self.assertEqual(edges, {("B1", "R1"): 10000,
                                 ("B2", "R1"): 5000,
                                 ("B2", "R2"): 5000})
        cons = json.loads(
            (out / "settle_report.json").read_text())["groups"][0][
                "conservation"]
        self.assertEqual(cons["receipt_unallocated_cents"], 1000)
        self.assertTrue(cons["bill_invariant_ok"])
        self.assertTrue(cons["receipt_invariant_ok"])

    def test_cancel_receipt_drops_its_unallocated_remainder(self):
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "100.00"),
            bill("b1", "2026-01-01T00:00:02Z", "B", "40.00", "2026-02-01"),
            # P has 40 allocated to B and 60 still unallocated.
            cancel_receipt("c1", "2026-01-01T00:00:03Z", "P"),
        ]
        out, _ = self.settle(rows, tag="drop")
        # B reopens to its full 40; the 60 unallocated disappears with P.
        bill_obj = next(o for o in self.lines(out, "objects.jsonl")
                        if o["object_id"] == "B")
        self.assertEqual(bill_obj["remaining_cents"], 4000)
        receipt_obj = next(o for o in self.lines(out, "objects.jsonl")
                           if o["object_id"] == "P")
        self.assertEqual(receipt_obj["remaining_cents"], 0)
        self.assertEqual(receipt_obj["status"], "cancelled")
        cons = json.loads(
            (out / "settle_report.json").read_text())["groups"][0][
                "conservation"]
        # Only B survives among bills; only no receipts survive, so the
        # receipt side collapses to zero while B is fully open.
        self.assertEqual(cons["bill_original_cents"], 4000)
        self.assertEqual(cons["bill_open_cents"], 4000)
        self.assertEqual(cons["receipt_original_cents"], 0)
        self.assertEqual(cons["active_allocation_cents"], 0)
        self.assertTrue(cons["bill_invariant_ok"])
        self.assertTrue(cons["receipt_invariant_ok"])

    def test_rematch_between_same_pair_creates_a_new_edge(self):
        # Cancel the receipt that covers B (releasing the B-P edge), then a
        # new receipt covers B again. A re-match involving the same bill is a
        # brand new edge with its own id; the released edge is preserved.
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "B", "100.00", "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "100.00"),
            cancel_receipt("c1", "2026-01-01T00:00:03Z", "P"),
            receipt("r2", "2026-01-01T00:00:04Z", "P2", "100.00"),
        ]
        out, _ = self.settle(rows, tag="rematch")
        edges = self.lines(out, "allocations.jsonl")
        old = [a for a in edges if (a["bill_id"], a["receipt_id"])
               == ("B", "P")]
        new = [a for a in edges if (a["bill_id"], a["receipt_id"])
               == ("B", "P2")]
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]["status"], RELEASED)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["status"], ACTIVE)
        self.assertNotEqual(old[0]["allocation_id"],
                            new[0]["allocation_id"])
        ids = [a["allocation_id"] for a in edges]
        self.assertEqual(len(ids), len(set(ids)))

    def test_standalone_bill_and_receipt_groups_settle_without_edges(self):
        # Different accounts -> different groups, so the two never match.
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "B", "10.00", "2026-02-01",
                 account="ACCT-B"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "7.00",
                    account="ACCT-R"),
        ]
        out, _ = self.settle(rows, tag="standalone")
        self.assertEqual(self.lines(out, "allocations.jsonl"), [])
        report = json.loads((out / "settle_report.json").read_text())
        self.assertEqual(report["group_count"], 2)
        for group in report["groups"]:
            self.assertTrue(
                group["conservation"]["bill_invariant_ok"]
                and group["conservation"]["receipt_invariant_ok"])

    def test_origin_meta_is_preserved_for_tracing(self):
        rows = [receipt("r1", "2026-01-01T00:00:01Z", "P", "7.00",
                        source_file="remote/feed.jsonl", line_number=42)]
        path = write_jsonl(self.dir / "origin.jsonl", rows)
        out = self.dir / "out-origin"
        run_settlement([path], config=self.config,
                       cutoff="2026-06-01T00:00:00Z", output_dir=out)
        ev = self.lines(out, "events.jsonl")[0]
        # The actual input file/line are recorded for re-runnable tracing...
        self.assertEqual(ev["source_file"], str(path))
        self.assertEqual(ev["line_number"], 1)
        # ...while the original meta/fields wrapper is kept verbatim.
        self.assertEqual(ev["meta"]["source_file"], "remote/feed.jsonl")
        self.assertEqual(ev["meta"]["line_number"], 42)
        self.assertEqual(ev["fields"]["ID"], "r1")


if __name__ == "__main__":
    unittest.main()
