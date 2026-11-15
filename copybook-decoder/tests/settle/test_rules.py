"""Settlement rule tests: ordering, cutoff, aging and exact arithmetic."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.settle import ACTIVE, SettleConfig, run_settlement

from .builders import (DEFAULT_CONFIG, bill, cancel_receipt,
                       receipt, split_files, write_jsonl)


class RulesTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="settle-rules-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = SettleConfig.from_dict(DEFAULT_CONFIG)

    def _settle(self, rows, cutoff="2026-06-01T00:00:00Z", files=None,
            tag="result"):
        if files is None:
            paths = [write_jsonl(self.dir / "events.jsonl", rows)]
        else:
            paths = files
        out = self.dir / tag
        summary = run_settlement(paths, config=self.config,
                                 cutoff=cutoff, output_dir=out)
        return out, summary

    def read_jsonl(self, out, name):
        return [json.loads(line) for line in
                (out / name).read_text(encoding="utf-8").splitlines()
                if line]

    def active_edges(self, out):
        return {(a["bill_id"], a["receipt_id"]): a["amount_cents"]
                for a in self.read_jsonl(out, "allocations.jsonl")
                if a["status"] == ACTIVE}

    # ----------------------------------------------------------- ordering
    def test_same_instant_uses_event_id_utf8_byte_order(self):
        # All four events share one instant; UTF-8 byte order of the ids
        # decides: bill B1 then B2, receipt P1 then P2.
        t = "2026-01-01T00:00:00Z"
        rows = [
            receipt("P2", t, "P2", "100.00"),
            bill("B2", t, "B2", "100.00", "2026-01-01"),
            receipt("P1", t, "P1", "100.00"),
            bill("B1", t, "B1", "100.00", "2026-01-01"),
        ]
        out, _ = self._settle(rows, tag="tie")
        # Earliest bill B1 takes earliest receipt P1; B2 then takes P2.
        self.assertEqual(self.active_edges(out),
                         {("B1", "P1"): 10000, ("B2", "P2"): 10000})

    def test_input_rearrangement_does_not_change_destination(self):
        t = "2026-01-01T00:00:00Z"
        rows = [
            bill("B1", t, "B1", "100.00", "2026-01-01"),
            bill("B2", t, "B2", "100.00", "2026-01-01"),
            receipt("P1", t, "P1", "150.00"),
            receipt("P2", t, "P2", "50.00"),
        ]
        out_a, _ = self._settle(list(rows), tag="ordered")
        shuffled = [rows[3], rows[1], rows[0], rows[2]]
        # Rebuild clean wrapper rows for the shuffled files.
        out_b, _ = self._settle(shuffled, tag="shuffled")
        self.assertEqual(
            self.read_jsonl(out_a, "allocations.jsonl"),
            self.read_jsonl(out_b, "allocations.jsonl"))
        self.assertEqual(
            self.read_jsonl(out_a, "objects.jsonl"),
            self.read_jsonl(out_b, "objects.jsonl"))

    def test_multiple_files_and_chunking_are_order_invariant(self):
        t = "2026-01-01T00:00:00Z"
        rows = [
            bill("B1", t, "B1", "80.00", "2026-01-01"),
            receipt("P1", t, "P1", "80.00"),
            bill("B2", t, "B2", "20.00", "2026-01-01"),
            receipt("P2", t, "P2", "20.00"),
        ]
        files = split_files(self.dir / "split", rows, [1, 2, 1])
        out, _ = self._settle(None, files=files, tag="multi")
        self.assertEqual(self.active_edges(out),
                         {("B1", "P1"): 8000, ("B2", "P2"): 2000})

    # ------------------------------------------------------------- offsets
    def test_non_utc_offsets_normalize_before_sorting(self):
        # 08:00+08:00 == 00:00 UTC; the two events are the same instant and
        # tie-break on event_id, regardless of the local spellings.
        rows = [
            bill("B1", "2026-01-01T08:00:00+08:00", "B1", "10.00",
                 "2026-01-01"),
            receipt("P1", "2026-01-01T03:00:00+03:00", "P1", "10.00"),
        ]
        out, summary = self._settle(rows, tag="offset")
        events = self.read_jsonl(out, "events.jsonl")
        self.assertEqual({e["event_id"]: e["at_utc"] for e in events},
                         {"B1": "2026-01-01T00:00:00+00:00",
                          "P1": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(self.active_edges(out), {("B1", "P1"): 1000})

    # -------------------------------------------------------------- cutoff
    def test_cutoff_inclusive_at_exact_event_instant(self):
        rows = [
            bill("B1", "2026-03-01T12:00:00Z", "B1", "10.00",
                 "2026-03-01"),
            receipt("P1", "2026-03-01T12:00:00Z", "P1", "10.00"),
        ]
        out, summary = self._settle(rows, cutoff="2026-03-01T12:00:00Z",
                                tag="inclusive")
        self.assertEqual(summary["applied_event_count"], 2)
        self.assertEqual(self.active_edges(out), {("B1", "P1"): 1000})

    def test_later_cancel_cannot_reach_into_cutoff_picture(self):
        rows = [
            bill("B1", "2026-01-01T00:00:00Z", "B1", "10.00",
                 "2026-01-01"),
            receipt("P1", "2026-01-01T00:00:01Z", "P1", "10.00"),
            cancel_receipt("P1-CANCEL", "2026-02-01T00:00:00Z", "P1"),
        ]
        out, summary = self._settle(rows, cutoff="2026-01-15T00:00:00Z",
                                tag="earlycut")
        # The cancel is after cutoff: the edge stays active, B1 stays clear.
        self.assertEqual(summary["applied_event_count"], 2)
        self.assertEqual(summary["post_cutoff_event_count"], 1)
        self.assertEqual(self.active_edges(out), {("B1", "P1"): 1000})
        events = {e["event_id"]: e for e in
                  self.read_jsonl(out, "events.jsonl")}
        self.assertTrue(events["P1-CANCEL"]["applied"] is False)
        self.assertIsNone(events["P1-CANCEL"]["effect"])

    def test_cutoff_base_day_drives_aging_not_event_time(self):
        # Bill due on the cutoff's UTC calendar day -> not overdue even
        # though it was opened weeks earlier.
        rows = [bill("B1", "2026-01-01T00:00:00Z", "B1", "10.00",
                     "2026-02-15")]
        out, _ = self._settle(rows, cutoff="2026-02-15T23:59:59+00:00",
                          tag="agingbase")
        report = json.loads((out / "settle_report.json").read_text())
        aging = report["groups"][0]["aging"]
        self.assertEqual(aging["base_day"], "2026-02-15")
        self.assertEqual(aging["bucket_cents"]["not_due"], 1000)

    # --------------------------------------------------------------- aging
    def test_all_five_aging_buckets_at_their_boundaries(self):
        base = "2026-03-31"
        # One fully open bill per boundary; aging uses base_day - due_on.
        dues = [
            ("B-FUT", "2026-04-01"),          # age -1 -> not_due
            ("B-D0", "2026-03-31"),           # age  0 -> not_due
            ("B-D30", "2026-03-01"),          # age 30 -> 1-30
            ("B-D31", "2026-02-28"),          # age 31 -> 31-60
            ("B-D61", "2026-01-29"),          # age 61 -> 61-90
            ("B-D91", "2025-12-30"),          # age 91 -> 91+
        ]
        rows = [bill(eid, f"2026-01-01T00:00:{i:02d}Z", eid, "100.00", due)
                for i, (eid, due) in enumerate(dues, start=1)]
        out, _ = self._settle(rows, cutoff="2026-03-31T12:00:00Z",
                              tag="buckets")
        report = json.loads((out / "settle_report.json").read_text())
        buckets = report["groups"][0]["aging"]["bucket_cents"]
        self.assertEqual(buckets["not_due"], 20000)        # two bills
        self.assertEqual(buckets["overdue_1_30"], 10000)
        self.assertEqual(buckets["overdue_31_60"], 10000)
        self.assertEqual(buckets["overdue_61_90"], 10000)
        self.assertEqual(buckets["overdue_91_plus"], 10000)
        detail = {d["bill_id"]: d for d in
                  report["groups"][0]["aging"]["details"]}
        self.assertEqual(detail["B-D30"]["age_days"], 30)
        self.assertEqual(detail["B-D31"]["age_days"], 31)

    def test_bucket_amounts_use_only_the_current_open_part(self):
        # Receipts fill earliest bills first: the first bills clear and the
        # residuals land on the latest bills.  Every bucketed amount is the
        # open remainder, never the original amount.
        dues = [
            ("B1", "2026-04-01"),            # not_due
            ("B2", "2026-03-31"),            # not_due
            ("B3", "2026-03-01"),            # 1-30
            ("B4", "2026-02-28"),            # 31-60
            ("B5", "2026-01-29"),            # 61-90
            ("B6", "2025-12-30"),            # 91+
        ]
        rows = [bill(eid, f"2026-01-01T00:00:{i:02d}Z", eid, "100.00", due)
                for i, (eid, due) in enumerate(dues, start=1)]
        # 440.00 available against 600.00 of bills: B1..B4 clear, B5 keeps
        # 60.00 open, B6 keeps all 100.00.
        rows.append(receipt("R1", "2026-02-01T00:00:00Z", "R1", "440.00"))
        out, _ = self._settle(rows, cutoff="2026-03-31T12:00:00Z",
                              tag="partial")
        report = json.loads((out / "settle_report.json").read_text())
        buckets = report["groups"][0]["aging"]["bucket_cents"]
        self.assertEqual(buckets["not_due"], 0)
        self.assertEqual(buckets["overdue_1_30"], 0)
        self.assertEqual(buckets["overdue_31_60"], 0)
        self.assertEqual(buckets["overdue_61_90"], 6000)
        self.assertEqual(buckets["overdue_91_plus"], 10000)
        # A fully covered bill still appears in aging details with zero.
        detail = {d["bill_id"]: d for d in
                  report["groups"][0]["aging"]["details"]}
        self.assertEqual(detail["B4"]["open_cents"], 0)
        self.assertEqual(detail["B5"]["open_cents"], 6000)

    # ----------------------------------------------------- receipts first
    def test_early_receipt_is_kept_and_used_by_later_bill(self):
        rows = [
            receipt("P1", "2026-01-01T00:00:00Z", "P1", "100.00"),
            bill("B1", "2026-01-05T00:00:00Z", "B1", "100.00",
                 "2026-02-01"),
        ]
        out, _ = self._settle(rows, tag="early")
        self.assertEqual(self.active_edges(out), {("B1", "P1"): 10000})

    def test_unallocated_receipt_remains_on_the_balance_sheet(self):
        rows = [
            receipt("P1", "2026-01-01T00:00:00Z", "P1", "100.00"),
            receipt("P2", "2026-01-01T00:00:01Z", "P2", "25.50"),
            bill("B1", "2026-01-02T00:00:00Z", "B1", "100.00",
                 "2026-02-01"),
        ]
        out, _ = self._settle(rows, tag="unalloc")
        report = json.loads((out / "settle_report.json").read_text())
        cons = report["groups"][0]["conservation"]
        # B1 fully covered by P1; P2's 25.50 stays unallocated.
        self.assertEqual(cons["receipt_unallocated_cents"], 2550)
        self.assertEqual(cons["bill_open_cents"], 0)
        self.assertTrue(cons["receipt_invariant_ok"])

    def test_amounts_beyond_64_bits_stay_exact(self):
        huge = "9" * 30 + ".00"           # 10^29-scale yuan
        rows = [
            bill("B1", "2026-01-01T00:00:00Z", "B1", huge, "2026-02-01"),
            receipt("P1", "2026-01-01T00:00:01Z", "P1", huge),
        ]
        out, _ = self._settle(rows, tag="huge")
        report = json.loads((out / "settle_report.json").read_text())
        cons = report["groups"][0]["conservation"]
        self.assertEqual(cons["bill_original_cents"], int("9" * 30) * 100)
        self.assertEqual(cons["active_allocation_cents"],
                         int("9" * 30) * 100)
        self.assertEqual(cons["bill_open_cents"], 0)
        edge = self.read_jsonl(out, "allocations.jsonl")[0]
        self.assertEqual(edge["amount"], huge)

    # ----------------------------------------------------------- currency
    def test_currencies_are_never_mixed(self):
        rows = [
            bill("B1", "2026-01-01T00:00:00Z", "B1", "10.00", "2026-02-01",
                 currency="CNY"),
            receipt("P1", "2026-01-01T00:00:01Z", "P1", "10.00",
                    currency="USD"),
        ]
        out, summary = self._settle(rows, tag="ccy")
        self.assertEqual(summary["group_count"], 2)
        # No edge crosses currencies; both groups hold an open item.
        self.assertEqual(self.read_jsonl(out, "allocations.jsonl"), [])
        report = json.loads((out / "settle_report.json").read_text())
        for group in report["groups"]:
            self.assertTrue(group["conservation"]["bill_invariant_ok"]
                            if group["currency"] == "CNY"
                            else group["conservation"]["receipt_invariant_ok"])


if __name__ == "__main__":
    unittest.main()
