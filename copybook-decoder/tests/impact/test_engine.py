"""run_impact_report: date ranges, problems, conclusions, output safety."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.impact import (COMPATIBLE, INCOMPATIBLE, UNVERIFIABLE,
                             ImpactError, run_impact_report)
from copybook.impact.engine import REPORT_JSON, REPORT_TXT

from tests.impact.builders import (NEW_V2, OLD_V1, build_main_scenario,
                                   entry, write_copybook, write_manifest)


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = self.dir / "report"

    def run_report(self, old_manifest, new_manifest):
        return run_impact_report(old_manifest, new_manifest, self.out)

    def report_json(self):
        return json.loads((self.out / REPORT_JSON).read_text(
            encoding="utf-8"))

    def key_block(self, report, sender, copybook="ACCT"):
        for block in report["senders"]:
            if block["sender"] == sender:
                for key in block["keys"]:
                    if key["copybook"] == copybook:
                        return key
        raise AssertionError(f"no key block for {sender}/{copybook}")


class TestMainScenario(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.old_manifest, self.new_manifest = build_main_scenario(self.dir)
        self.summary = self.run_report(self.old_manifest, self.new_manifest)
        self.report = self.report_json()

    def test_summary_and_files(self):
        self.assertEqual(self.summary["conclusion"], INCOMPATIBLE)
        self.assertEqual(self.summary["problem_count"], 0)
        self.assertTrue((self.out / REPORT_TXT).exists())
        self.assertEqual(self.summary["sender_count"], 2)

    def test_sources_preserved(self):
        self.assertEqual(self.report["sources"]["old_manifest"],
                         str(self.old_manifest))
        self.assertEqual(self.report["sources"]["new_manifest"],
                         str(self.new_manifest))

    def test_banka_affected_range_closed_intervals(self):
        key = self.key_block(self.report, "BANKA")
        self.assertEqual(key["affected_ranges"], [{
            "from": "2024-09-01", "to": None, "old_version": "1",
            "new_version": "2", "conclusion": INCOMPATIBLE,
            "comparison": 0}])
        # effective_to is inclusive: v1 stays valid through 2024-08-31
        self.assertEqual(key["unaffected_ranges"], [{
            "from": "2024-01-01", "to": "2024-08-31", "version": "1"}])

    def test_bankb_unaffected(self):
        key = self.key_block(self.report, "BANKB")
        self.assertEqual(key["affected_ranges"], [])
        self.assertEqual(key["unaffected_ranges"], [{
            "from": "2024-01-01", "to": None, "version": "1"}])
        block = next(b for b in self.report["senders"]
                     if b["sender"] == "BANKB")
        self.assertEqual(block["status"], "unaffected")

    def test_field_diff_content(self):
        key = self.key_block(self.report, "BANKA")
        cmp = key["comparisons"][0]
        cats = {f["path"]: f["category"] for f in cmp["fields"]}
        self.assertEqual(cats, {
            "ACCT-RECORD/ACCT-ID": "unchanged",
            "ACCT-RECORD/ACCT-NAME": "unchanged",
            "ACCT-RECORD/BALANCE": "reinterpreted",
            "ACCT-RECORD/LAST-TXN-DATE": "moved",
            "ACCT-RECORD/LEGACY-CODE": "moved",
            "ACCT-RECORD/LEGACY-CODE-NUM": "moved",
            "ACCT-RECORD/OLD-STATUS": "removed",
            "ACCT-RECORD/BALANCE-TEXT": "added",
            "ACCT-RECORD/CURRENCY": "added",
        })
        fields = {f["path"]: f for f in cmp["fields"]}
        balance = fields["ACCT-RECORD/BALANCE"]
        self.assertEqual(balance["old"]["bytes"], [30, 35])
        self.assertEqual(balance["new"]["bytes"], [30, 35])
        self.assertEqual(balance["old"]["pic"], "S9(7)V99")
        self.assertEqual(balance["new"]["pic"], "S9(6)V999")
        moved = fields["ACCT-RECORD/LAST-TXN-DATE"]
        self.assertEqual(moved["old"]["bytes"], [35, 43])
        self.assertEqual(moved["new"]["bytes"], [38, 46])
        view = fields["ACCT-RECORD/BALANCE-TEXT"]
        self.assertEqual(view["new"]["redefines"], "ACCT-RECORD/BALANCE")
        self.assertEqual(cmp["record_length"]["old"], 50)
        self.assertEqual(cmp["record_length"]["new"], 51)
        delta = sum(i["bytes"]
                    for i in cmp["record_length"]["explanation"])
        self.assertEqual(delta, 1)
        self.assertEqual(cmp["conclusion"], INCOMPATIBLE)


class TestDateBoundaries(EngineTestCase):
    def _scenario(self, new_layouts, old_layouts=None):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(old_dir, "ACCT-2.cpy", NEW_V2)
        write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-2.cpy", NEW_V2)
        old_manifest = write_manifest(old_dir, old_layouts or [entry()])
        new_manifest = write_manifest(new_dir, new_layouts)
        self.run_report(old_manifest, new_manifest)
        return self.key_block(self.report_json(), "BANKA")

    def test_switch_on_boundary_day(self):
        key = self._scenario([
            entry(effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01"),
        ])
        self.assertEqual(key["affected_ranges"][0]["from"], "2024-09-01")
        self.assertEqual(key["unaffected_ranges"][0]["to"], "2024-08-31")

    def test_multiple_transitions_produce_separate_ranges(self):
        key = self._scenario(
            old_layouts=[
                entry(effective_to="2024-08-31"),
                entry(version="2", effective_from="2024-09-01"),
            ],
            new_layouts=[
                entry(effective_to="2024-03-31"),
                entry(version="2", effective_from="2024-04-01",
                      effective_to="2024-06-30"),
                entry(version="1", effective_from="2024-07-01"),
            ])
        affected = [(r["from"], r["to"], r["old_version"], r["new_version"])
                    for r in key["affected_ranges"]]
        self.assertEqual(affected, [("2024-04-01", "2024-06-30", "1", "2"),
                                    ("2024-09-01", None, "2", "1")])
        unaffected = [(r["from"], r["to"]) for r in key["unaffected_ranges"]]
        self.assertEqual(unaffected, [("2024-01-01", "2024-03-31"),
                                      ("2024-07-01", "2024-08-31")])

    def test_each_version_pair_gets_one_comparison(self):
        key = self._scenario(
            old_layouts=[
                entry(effective_to="2024-08-31"),
                entry(version="2", effective_from="2024-09-01"),
            ],
            new_layouts=[
                entry(effective_to="2024-03-31"),
                entry(version="2", effective_from="2024-04-01",
                      effective_to="2024-06-30"),
                entry(version="1", effective_from="2024-07-01"),
            ])
        # v1->v2 and v2->v1 are two distinct version pairs
        self.assertEqual(len(key["comparisons"]), 2)


class TestProblems(EngineTestCase):
    def _run(self, old_layouts, new_layouts, extra_new_copybooks=None):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-2.cpy", NEW_V2)
        for name, text in (extra_new_copybooks or {}).items():
            write_copybook(new_dir, name, text)
        old_manifest = write_manifest(old_dir, old_layouts)
        new_manifest = write_manifest(new_dir, new_layouts)
        summary = self.run_report(old_manifest, new_manifest)
        return summary, self.report_json()

    def test_overlap_is_unverifiable(self):
        summary, report = self._run(
            [entry()],
            [entry(effective_to="2024-08-31"),
             entry(version="2", effective_from="2024-08-15")])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "overlap")
        self.assertEqual(prob["source"], "new_manifest")
        self.assertEqual(prob["sender"], "BANKA")
        self.assertEqual(prob["range"],
                         {"from": "2024-08-15", "to": "2024-08-31"})
        key = self.key_block(report, "BANKA")
        reasons = [r["reason"] for r in key["unresolvable_ranges"]]
        self.assertEqual(reasons, ["overlap"])
        # the non-overlapping tail is still compared, not silently dropped
        self.assertEqual(len(key["affected_ranges"]), 1)
        self.assertEqual(key["affected_ranges"][0]["from"], "2024-09-01")

    def test_gap_in_new_manifest(self):
        summary, report = self._run(
            [entry()],
            [entry(effective_to="2024-08-31"),
             entry(version="2", effective_from="2024-10-01")])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "gap")
        self.assertEqual(prob["source"], "new_manifest")
        self.assertEqual(prob["range"],
                         {"from": "2024-09-01", "to": "2024-09-30"})

    def test_missing_old_baseline(self):
        summary, report = self._run(
            [entry()],
            [entry(effective_from="2023-06-01",
                   effective_to="2024-08-31"),
             entry(version="2", effective_from="2024-09-01")])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "no_old_baseline")
        self.assertEqual(prob["source"], "old_manifest")
        self.assertEqual(prob["range"],
                         {"from": "2023-06-01", "to": "2023-12-31"})

    def test_sender_only_in_new_manifest(self):
        summary, report = self._run(
            [entry()],
            [entry(), entry(sender="BANKC", copybook="CARD",
                            version="1", file="copybooks/ACCT-1.cpy")])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "no_old_baseline")
        self.assertEqual(prob["sender"], "BANKC")
        self.assertEqual(prob["copybook"], "CARD")

    def test_copybook_not_found(self):
        summary, report = self._run(
            [entry()],
            [entry(effective_to="2024-08-31"),
             entry(version="9", effective_from="2024-09-01")])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "copybook_not_found")
        self.assertEqual(prob["source"], "new_manifest")
        self.assertEqual(prob["sender"], "BANKA")
        self.assertEqual(prob["range"], {"from": "2024-09-01", "to": None})
        key = self.key_block(report, "BANKA")
        self.assertEqual(key["unresolvable_ranges"][0]["reason"],
                         "copybook_unavailable")
        self.assertEqual(key["comparisons"], [])

    def test_copybook_parse_error(self):
        summary, report = self._run(
            [entry()],
            [entry(effective_to="2024-08-31"),
             entry(version="2", effective_from="2024-09-01")],
            extra_new_copybooks={"ACCT-2.cpy": "01  BROKEN.\n  05  X PIC Z(3).\n"})
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "copybook_parse_error")

    def test_duplicate_field_path(self):
        dup = ("      01  ACCT-RECORD.\n"
               "          05  GRP.\n"
               "              10  CODE  PIC X(2).\n"
               "          05  GRP.\n"
               "              10  CODE  PIC 9(2).\n")
        summary, report = self._run(
            [entry()],
            [entry(effective_to="2024-08-31"),
             entry(version="2", effective_from="2024-09-01")],
            extra_new_copybooks={"ACCT-2.cpy": dup})
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        kinds = {p["kind"] for p in report["problems"]}
        self.assertEqual(kinds, {"duplicate_field_path"})
        paths = {p["path"] for p in report["problems"]}
        self.assertIn("ACCT-RECORD/GRP/CODE", paths)
        for prob in report["problems"]:
            self.assertEqual(prob["sender"], "BANKA")
            self.assertEqual(prob["source"], "new_manifest")

    def test_version_reused_for_different_layout(self):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-1.cpy", NEW_V2)  # same version!
        old_manifest = write_manifest(old_dir, [entry()])
        new_manifest = write_manifest(new_dir, [entry()])
        summary = self.run_report(old_manifest, new_manifest)
        report = self.report_json()
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        (prob,) = report["problems"]
        self.assertEqual(prob["kind"], "version_reused")
        self.assertEqual(prob["sender"], "BANKA")

    def test_manifest_unreadable(self):
        old_manifest = self.dir / "old" / "manifest.json"
        old_manifest.parent.mkdir(parents=True)
        old_manifest.write_text("{not json", encoding="utf-8")
        new_dir = self.dir / "new"
        write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
        new_manifest = write_manifest(new_dir, [entry()])
        summary = self.run_report(old_manifest, new_manifest)
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        kinds = {p["kind"] for p in self.report_json()["problems"]}
        self.assertIn("manifest_unreadable", kinds)

    def test_invalid_entry_recorded_not_raised(self):
        summary, report = self._run(
            [entry()],
            [{"sender": "BANKA"},  # missing required keys
             entry()])
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        kinds = {p["kind"] for p in report["problems"]}
        self.assertIn("manifest_entry_invalid", kinds)


class TestCompatibleChange(EngineTestCase):
    def test_view_and_filler_fill_is_compatible(self):
        old = ("      01  CARD-RECORD.\n"
               "          05  CARD-ID   PIC X(6).\n"
               "          05  AMOUNT    PIC S9(5)V99 COMP-3.\n"
               "          05  FILLER    PIC X(3).\n")
        new = ("      01  CARD-RECORD.\n"
               "          05  CARD-ID   PIC X(6).\n"
               "          05  AMOUNT    PIC S9(5)V99 COMP-3.\n"
               "          05  AMT-TEXT  REDEFINES AMOUNT PIC X(4).\n"
               "          05  REGION    PIC X(3).\n")
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "CARD-1.cpy", old)
        write_copybook(new_dir, "CARD-1.cpy", old)
        write_copybook(new_dir, "CARD-2.cpy", new)
        old_manifest = write_manifest(old_dir, [
            entry(sender="BANKC", copybook="CARD")])
        new_manifest = write_manifest(new_dir, [
            entry(sender="BANKC", copybook="CARD",
                  effective_to="2024-08-31"),
            entry(sender="BANKC", copybook="CARD", version="2",
                  effective_from="2024-09-01")])
        summary = self.run_report(old_manifest, new_manifest)
        self.assertEqual(summary["conclusion"], COMPATIBLE)
        key = self.key_block(self.report_json(), "BANKC", "CARD")
        self.assertEqual(key["affected_ranges"][0]["conclusion"],
                         COMPATIBLE)


class TestOutputSafety(EngineTestCase):
    def test_existing_output_dir_refused(self):
        self.out.mkdir()
        sentinel = self.out / "keep.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        old_manifest, new_manifest = build_main_scenario(self.dir)
        with self.assertRaises(ImpactError):
            run_impact_report(old_manifest, new_manifest, self.out)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertFalse((self.out / REPORT_JSON).exists())

    def test_problems_still_produce_a_report_directory(self):
        old_dir, new_dir = self.dir / "old", self.dir / "new"
        write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
        write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
        old_manifest = write_manifest(old_dir, [entry()])
        # gap: nothing covers 2024-09
        new_manifest = write_manifest(new_dir, [
            entry(effective_to="2024-08-31"),
            entry(version="1", effective_from="2024-10-01"),
        ])
        summary = run_impact_report(old_manifest, new_manifest, self.out)
        self.assertEqual(summary["conclusion"], UNVERIFIABLE)
        self.assertTrue((self.out / REPORT_JSON).exists())
        self.assertTrue((self.out / REPORT_TXT).exists())


if __name__ == "__main__":
    unittest.main()
