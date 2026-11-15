"""Error containment and fatal-input tests for settlement.

Two failure levels are exercised exactly as the contract requires:

* *group-local business faults* (bad amount/date, duplicate object id,
  wrong-kind/cross-group/forward/double cancels) invalidate only their own
  (account, currency) group: that group delivers no balances, aging or
  active allocations, keeps its raw events and errors, while every other
  group still settles correctly -- even when the fault is the last event;
* *run-level faults* (bad JSON, missing routing fields, duplicate
  event_id, bad config, an existing output directory) abort the whole run
  and leave no formal result behind.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.settle import (SettleConfig, SettleConfigError,
                             SettleInputError, run_settlement, trace_object)

from .builders import (DEFAULT_CONFIG, bill, cancel_bill, cancel_receipt,
                       receipt, write_jsonl)


class ErrorCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="settle-errors-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = SettleConfig.from_dict(DEFAULT_CONFIG)

    def _settle(self, rows, cutoff="2026-06-01T00:00:00Z", tag="result",
                inputs=None):
        if inputs is None:
            inputs = [write_jsonl(self.dir / "events.jsonl", rows)]
        out = self.dir / tag
        return run_settlement(inputs, config=self.config, cutoff=cutoff,
                              output_dir=out), out

    def _lines(self, out, name):
        path = out / name
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line]

    # ------------------------------------------------- group-local faults
    def test_invalid_group_delivers_no_balances_but_keeps_events(self):
        good = [
            bill("g1", "2026-01-01T00:00:01Z", "GB", "10.00", "2026-02-01",
                 account="GOOD"),
            receipt("g2", "2026-01-01T00:00:02Z", "GP", "10.00",
                    account="GOOD"),
        ]
        bad = [
            bill("b1", "2026-01-01T00:00:01Z", "XB", "10.00", "2026-02-01",
                 account="BAD"),
            # zero amount: illegal business field
            receipt("b2", "2026-01-01T00:00:02Z", "XP", "0.00",
                    account="BAD"),
        ]
        (summary, out) = self._settle(good + bad, tag="mixed")
        self.assertEqual(summary["conclusion"], "business_rejected")
        self.assertEqual(summary["valid_group_count"], 1)
        self.assertEqual(summary["invalid_group_count"], 1)
        # The good group still settled end to end.
        self.assertEqual(
            {(a["bill_id"], a["receipt_id"])
             for a in self._lines(out, "allocations.jsonl")},
            {("GB", "GP")})
        # No object row, aging block or active edge exists for BAD.
        self.assertEqual(
            [o for o in self._lines(out, "objects.jsonl")
             if o["account"] == "BAD"], [])
        invalid = self._lines(out, "invalid_groups.jsonl")
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["account"], "BAD")
        self.assertEqual(invalid[0]["errors"][0]["code"], "invalid_amount")
        self.assertEqual(invalid[0]["errors"][0]["field_route"], "amount")
        self.assertIn("line_number", invalid[0]["errors"][0])
        # The bad group's events are still retained, flagged group_valid=false.
        bad_events = [e for e in self._lines(out, "events.jsonl")
                      if e["account"] == "BAD"]
        self.assertEqual({e["event_id"] for e in bad_events}, {"b1", "b2"})
        self.assertTrue(all(e["group_valid"] is False for e in bad_events))

    def test_error_at_group_end_still_poisons_the_whole_group(self):
        rows = [
            bill("x1", "2026-01-01T00:00:01Z", "XB", "10.00", "2026-02-01"),
            receipt("x2", "2026-01-01T00:00:02Z", "XP", "10.00"),
            # last event cancels a non-existent bill
            cancel_bill("x3", "2026-01-01T00:00:03Z", "GHOST"),
        ]
        (summary, out) = self._settle(rows, tag="endfault")
        self.assertEqual(summary["invalid_group_count"], 1)
        # No partial balances may be presented for this group.
        self.assertEqual(self._lines(out, "objects.jsonl"), [])
        self.assertEqual(self._lines(out, "allocations.jsonl"), [])
        report = json.loads((out / "settle_report.json").read_text())
        group = report["groups"][0]
        self.assertEqual(group["status"], "invalid")
        self.assertNotIn("conservation", group)
        self.assertNotIn("aging", group)
        self.assertEqual(group["errors"][0]["code"], "unknown_target")

    def test_double_cancel_is_rejected(self):
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00"),
            cancel_receipt("c1", "2026-01-01T00:00:02Z", "P"),
            cancel_receipt("c2", "2026-01-01T00:00:03Z", "P"),
        ]
        (_, out) = self._settle(rows, tag="double")
        invalid = self._lines(out, "invalid_groups.jsonl")
        self.assertEqual(invalid[0]["errors"][0]["code"],
                         "already_cancelled")

    def test_forward_reference_is_rejected(self):
        # The cancel is ordered before its target's opening; the target
        # therefore does not exist yet at application time.
        rows = [
            cancel_bill("c1", "2026-01-01T00:00:01Z", "LATER"),
            bill("b1", "2026-01-01T00:00:02Z", "LATER", "10.00",
                 "2026-02-01"),
        ]
        (_, out) = self._settle(rows, tag="forward")
        invalid = self._lines(out, "invalid_groups.jsonl")
        self.assertEqual(invalid[0]["errors"][0]["code"], "unknown_target")

    def test_wrong_kind_cancel_targets_other_namespace(self):
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00"),
            # A bill-cancel cannot name a receipt.
            cancel_bill("c1", "2026-01-01T00:00:02Z", "P"),
        ]
        (_, out) = self._settle(rows, tag="wrongkind")
        invalid = self._lines(out, "invalid_groups.jsonl")
        self.assertEqual(invalid[0]["errors"][0]["code"], "unknown_target")

    def test_cross_account_reference_is_rejected(self):
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00",
                    account="ACC-A"),
            cancel_receipt("c1", "2026-01-01T00:00:02Z", "P",
                           account="ACC-B"),
        ]
        (_, out) = self._settle(rows, tag="cross")
        invalid = self._lines(out, "invalid_groups.jsonl")
        codes = {e["code"] for rec in invalid for e in rec["errors"]}
        self.assertIn("unknown_target", codes)

    def test_reused_object_id_even_after_cancel_is_rejected(self):
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00"),
            cancel_receipt("c1", "2026-01-01T00:00:02Z", "P"),
            receipt("r2", "2026-01-01T00:00:03Z", "P", "10.00"),
        ]
        (_, out) = self._settle(rows, tag="reuse")
        invalid = self._lines(out, "invalid_groups.jsonl")
        self.assertEqual(invalid[0]["errors"][0]["code"],
                         "duplicate_object_id")

    def test_bad_due_on_is_localized_to_route(self):
        rows = [bill("b1", "2026-01-01T00:00:01Z", "B", "10.00",
                     "2026-02-30")]
        (_, out) = self._settle(rows, tag="baddue")
        invalid = self._lines(out, "invalid_groups.jsonl")
        error = invalid[0]["errors"][0]
        self.assertEqual(error["code"], "invalid_due_on")
        self.assertEqual(error["field_route"], "due_on")
        self.assertEqual(error["detail"]["actual"], "2026-02-30")

    def test_object_id_007_is_never_numericized(self):
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "007", "10.00",
                 "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "007", "10.00"),
        ]
        (_, out) = self._settle(rows, tag="ids")
        # Separate namespaces: both objects keep the literal "007".
        objects = self._lines(out, "objects.jsonl")
        ids = {(o["kind"], o["object_id"]) for o in objects}
        self.assertIn(("BILL", "007"), ids)
        self.assertIn(("RECEIPT", "007"), ids)
        edge = self._lines(out, "allocations.jsonl")[0]
        self.assertEqual(edge["bill_id"], "007")
        self.assertEqual(edge["receipt_id"], "007")

    def test_post_cutoff_business_fault_does_not_invalidate(self):
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "B", "10.00", "2026-02-01"),
            receipt("r1", "2026-01-01T00:00:02Z", "P", "10.00"),
            # Illegal double cancel, but after the cutoff: not executed.
            cancel_receipt("c1", "2026-02-01T00:00:01Z", "P"),
            cancel_receipt("c2", "2026-02-01T00:00:02Z", "P"),
        ]
        (summary, out) = self._settle(rows, cutoff="2026-01-15T00:00:00Z",
                                      tag="postcut")
        self.assertEqual(summary["invalid_group_count"], 0)
        self.assertEqual(summary["conclusion"], "settled")
        self.assertEqual(len(self._lines(out, "allocations.jsonl")), 1)
        self.assertFalse((out / "invalid_groups.jsonl").exists())

    # ------------------------------------------------------- run-level
    def _raw_file(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_bad_json_aborts_and_leaves_no_result(self):
        path = self._raw_file("broken.jsonl",
                              json.dumps({"meta": {}, "fields": {}})
                              + "\n{not json\n")
        with self.assertRaises(SettleInputError):
            run_settlement([path], config=self.config,
                           cutoff="2026-06-01T00:00:00Z",
                           output_dir=self.dir / "nout")
        self.assertFalse((self.dir / "nout").exists())

    def test_duplicate_event_id_aborts_whole_run(self):
        rows = [
            receipt("DUP", "2026-01-01T00:00:01Z", "P1", "10.00"),
            bill("DUP", "2026-01-01T00:00:02Z", "B1", "10.00",
                 "2026-02-01"),
        ]
        path = write_jsonl(self.dir / "dup.jsonl", rows)
        with self.assertRaises(SettleInputError):
            run_settlement([path], config=self.config,
                           cutoff="2026-06-01T00:00:00Z",
                           output_dir=self.dir / "nout")
        self.assertFalse((self.dir / "nout").exists())

    def test_missing_routing_field_aborts_whole_run(self):
        bad = {"meta": {"source_file": "x", "line_number": 1},
               "fields": {"ID": "e1", "ACCT": "A", "CCY": "CNY",
                          "KIND": "RECEIPT"}}   # no AT
        path = self._raw_file("noroute.jsonl", json.dumps(bad) + "\n")
        with self.assertRaises(SettleInputError):
            run_settlement([path], config=self.config,
                           cutoff="2026-06-01T00:00:00Z",
                           output_dir=self.dir / "nout")

    def test_duplicate_event_id_after_cutoff_still_aborts(self):
        # Routing uniqueness covers the whole input, even post-cutoff.
        rows = [
            receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00"),
            receipt("DUP", "2026-01-01T00:00:02Z", "Q", "10.00"),
            bill("DUP", "2027-01-01T00:00:00Z", "B", "10.00", "2027-02-01"),
        ]
        path = write_jsonl(self.dir / "dupfuture.jsonl", rows)
        with self.assertRaises(SettleInputError):
            run_settlement([path], config=self.config,
                           cutoff="2026-06-01T00:00:00Z",
                           output_dir=self.dir / "nout")

    def test_bad_config_is_rejected(self):
        all_routes = ("event_id", "account", "currency", "at", "kind",
                      "object_id", "amount", "due_on", "target_id")
        with self.assertRaises(SettleConfigError):
            SettleConfig.from_dict({"paths": {"event_id": ["ID"]}})
        with self.assertRaises(SettleConfigError):
            SettleConfig.from_dict({"paths": {
                name: ["X"] for name in all_routes if name != "kind"}})
        with self.assertRaises(SettleConfigError):
            SettleConfig.from_dict({"paths": {
                **{name: ["X"] for name in all_routes},
                "unknown_route": ["Y"]}})

    def test_existing_output_directory_is_refused_and_untouched(self):
        rows = [receipt("r1", "2026-01-01T00:00:01Z", "P", "10.00")]
        path = write_jsonl(self.dir / "ev.jsonl", rows)
        out = self.dir / "taken"
        out.mkdir()
        sentinel = out / "keep.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        with self.assertRaises(SettleInputError):
            run_settlement([path], config=self.config,
                           cutoff="2026-06-01T00:00:00Z", output_dir=out)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["keep.txt"])

    def test_trace_points_to_invalid_group_instead_of_faking_balance(self):
        rows = [
            bill("b1", "2026-01-01T00:00:01Z", "XB", "10.00", "2026-02-01"),
            receipt("b2", "2026-01-01T00:00:02Z", "XP", "0.00"),
        ]
        (_, out) = self._settle(rows, tag="tracebad")
        from copybook.settle import SettleQueryError
        with self.assertRaises(SettleQueryError):
            trace_object(out, "XB", kind="BILL")


if __name__ == "__main__":
    unittest.main()
