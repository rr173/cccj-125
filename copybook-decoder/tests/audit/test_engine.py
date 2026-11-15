"""State-machine tests for the handoff validation engine.

Evidence is assembled directly as decoded JSONL/quarantine objects (the
shape ``write_jsonl`` / ``export_quarantine`` produce); a separate
end-to-end script drives the real EBCDIC decoder before auditing.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.audit import (AuditConfig, BUSINESS_REJECTED, PASS,
                            run_audit)
from copybook.audit import AuditInputError

SRC = "/landing/feed.dat"
CATEGORIES = ["AAA", "BBB"]


def _config():
    return AuditConfig.from_dict({
        "categories": CATEGORIES,
        "paths": {
            "record_type": ["REC-TYPE"],
            "file_id": ["FILE-ID"],
            "batch_id": ["BATCH-ID"],
            "seq": ["SEQ"],
            "category": ["CATEGORY"],
            "quantity": ["QTY"],
            "btail_detail_count": ["DETAIL-COUNT"],
            "btail_totals": {"AAA": ["TOTAL-AAA"], "BBB": ["TOTAL-BBB"]},
            "ftail_batch_count": ["BATCH-COUNT"],
            "ftail_detail_count": ["DETAIL-COUNT"],
        },
    })


class Builder:
    """Assembles physical lines, tracking byte ranges automatically."""

    def __init__(self):
        self.rows = []          # (kind, dict)
        self._pos = 0
        self._line = 0

    def _bump(self, width=20):
        start = self._pos
        end = start + width
        self._pos = end + 1     # newline
        self._line += 1
        return self._line, start, end

    def rec(self, rtype, **fields):
        line, start, end = self._bump()
        self.rows.append(("record", {
            "meta": {"source_file": SRC, "line_number": line,
                     "byte_start": start, "byte_end": end,
                     "layout": "X", "layout_version": "1"},
            "fields": {"REC-TYPE": rtype, **fields},
        }))
        return line

    def quar(self, reason="decode"):
        line, start, end = self._bump()
        self.rows.append(("quarantine", {
            "source_file": SRC, "line_number": line,
            "byte_start": start, "byte_end": end,
            "reason": reason, "error": "bad", "raw_hex": "ff" * 20,
        }))
        return line

    def write(self, d):
        recs = [obj for kind, obj in self.rows if kind == "record"]
        quars = [obj for kind, obj in self.rows if kind == "quarantine"]
        rp = Path(d) / "records.jsonl"
        qp = Path(d) / "quarantine.jsonl"
        rp.write_text("".join(json.dumps(o) + "\n" for o in recs),
                      encoding="utf-8")
        qp.write_text("".join(json.dumps(o) + "\n" for o in quars),
                      encoding="utf-8")
        return rp, qp, self._line


# Convenience constructors for the five control records.
def fhead(file_id="F1"):
    return ("FHEAD", {"FILE-ID": file_id})


def ftail(file_id="F1", batch_count=1, detail_count=1):
    return ("FTAIL", {"FILE-ID": file_id,
                      "BATCH-COUNT": str(batch_count),
                      "DETAIL-COUNT": str(detail_count)})


def bhead(batch_id="B1"):
    return ("BHEAD", {"BATCH-ID": batch_id})


def detail(seq, batch_id="B1", category="AAA", quantity="1.000"):
    return ("DETAIL", {"BATCH-ID": batch_id, "SEQ": str(seq),
                       "CATEGORY": category, "QTY": quantity})


def btail(count, total_aaa="1.000", total_bbb="0.000", batch_id="B1"):
    return ("BTAIL", {"BATCH-ID": batch_id,
                      "DETAIL-COUNT": str(count),
                      "TOTAL-AAA": total_aaa,
                      "TOTAL-BBB": total_bbb})


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = _config()

    def audit(self, b: Builder, *, expected_lines=None):
        rp, qp, total = b.write(self.dir)
        out = self.dir / "delivery"
        tmp = self.dir / "staging"
        summary = run_audit(
            rp, qp, config=self.cfg, expected_source=SRC,
            expected_lines=expected_lines or total,
            output_dir=out, temp_dir=tmp)
        # The Python result is a bounded summary; the complete report is
        # streamed to disk and is what assertions inspect.
        report = json.loads((out / "audit_report.json").read_text())
        return report, out, tmp

    def build(self, triples):
        b = Builder()
        for t in triples:
            rtype, fields = t
            b.rec(rtype, **fields)
        return b


class TestCleanFiles(EngineTestCase):
    def test_single_good_batch_passes(self):
        b = self.build([fhead(), bhead(), detail(1),
                        btail(1), ftail()])
        report, out, tmp = self.audit(b)
        self.assertEqual(report["conclusion"], PASS)
        self.assertEqual(report["batches"][0]["status"], "accepted")
        self.assertEqual(report["accepted_detail_line_numbers"], [3])
        self.assertTrue((out / "accepted_details.jsonl").exists())
        payload = json.loads((out / "accepted_details.jsonl").read_text())
        # original meta/fields preserved, ids annotated, nothing overwritten
        self.assertEqual(payload["meta"]["line_number"], 3)
        self.assertEqual(payload["fields"]["QTY"], "1.000")
        self.assertEqual(payload["file_id"], "F1")
        self.assertEqual(payload["batch_id"], "B1")
        self.assertNotIn("file_id", payload["fields"])
        # staging cleaned up, delivery retained
        self.assertFalse(tmp.exists() and any(tmp.iterdir()))

    def test_empty_batch_is_allowed(self):
        b = self.build([fhead(), bhead("E1"),
                        btail(0, "0.000", "0.000"),
                        ftail(batch_count=1, detail_count=0)])
        report, _, _ = self.audit(b)
        self.assertEqual(report["conclusion"], PASS)
        self.assertEqual(report["batches"][0]["status"], "accepted")
        self.assertEqual(report["accepted_detail_line_numbers"], [])

    def test_two_good_batches_seq_restarts(self):
        b = self.build([
            fhead(),
            bhead("B1"), detail(1, "B1", "AAA", "1.000"),
            detail(2, "B1", "BBB", "2.500"),
            btail(2, "1.000", "2.500"),
            bhead("B2"), detail(1, "B2", "AAA", "-3.500"),
            btail(1, "-3.500", "0.000", batch_id="B2"),
            ftail(batch_count=2, detail_count=3),
        ])
        report, out, _ = self.audit(b)
        self.assertEqual(report["conclusion"], PASS)
        self.assertEqual([x["batch_id"] for x in report["batches"]],
                         ["B1", "B2"])
        self.assertEqual(report["accepted_detail_line_numbers"],
                         [3, 4, 7])
        # exact signed/three-decimal totals
        totals = report["batches"][0]["category_totals"]
        self.assertEqual([t["actual_milli"] for t in totals], [1000, 2500])

    def test_negative_quantities_cancel_exactly(self):
        b = self.build([
            fhead(), bhead(),
            detail(1, quantity="10.001"), detail(2, quantity="-10.001"),
            btail(2, "0.000", "0.000"),
            ftail(batch_count=1, detail_count=2),
        ])
        report, _, _ = self.audit(b)
        self.assertEqual(report["conclusion"], PASS)
        aaa = report["batches"][0]["category_totals"][0]
        self.assertEqual(aaa["actual_milli"], 0)


class TestBatchLocalRejection(EngineTestCase):
    def _two_batch_file(self, first_batch_triples,
                        ftail_detail_count=2):
        triples = [fhead(), bhead("B1")]
        triples += first_batch_triples
        triples += [
            bhead("B2"), detail(1, "B2", "AAA", "4.000"),
            btail(1, "4.000", "0.000", batch_id="B2"),
            ftail(batch_count=2, detail_count=ftail_detail_count),
        ]
        return self.build(triples)

    def test_quarantine_in_first_batch_rejects_it_second_still_accepted(self):
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.rec("BHEAD", **{"BATCH-ID": "B1"})
        qline = b.quar()                       # bad detail position in B1
        b.rec("BTAIL", **{"BATCH-ID": "B1", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "0.000", "TOTAL-BBB": "0.000"})
        b.rec("BHEAD", **{"BATCH-ID": "B2"})
        good = b.rec("DETAIL", **{"BATCH-ID": "B2", "SEQ": "1",
                                  "CATEGORY": "AAA", "QTY": "4.000"})
        b.rec("BTAIL", **{"BATCH-ID": "B2", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "4.000", "TOTAL-BBB": "0.000"})
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "2",
                          "DETAIL-COUNT": "2"})   # quarantine counts too
        report, _, _ = self.audit(b)
        self.assertEqual(report["conclusion"], BUSINESS_REJECTED)
        b1, b2 = report["batches"]
        self.assertEqual(b1["status"], "rejected")
        self.assertEqual(b2["status"], "accepted")
        self.assertTrue(b1["quantity_check"]["status"] == "undecidable")
        self.assertEqual(b1["detail_count"]["actual"], 1)
        # accepted details come only from the good batch
        self.assertEqual(report["accepted_detail_line_numbers"], [good])
        # quarantine line number is retained in diagnostics
        codes = {d["code"] for d in b1["diagnostics"]}
        self.assertIn("quarantined_detail", codes)
        diag = next(d for d in b1["diagnostics"]
                    if d["code"] == "quarantined_detail")
        self.assertEqual(diag["line_number"], qline)

    def test_bad_seq_rejects_batch_but_later_batch_survives(self):
        b = self._two_batch_file([
            detail(1), detail(2), detail(2),     # seq jumps (1,2,2)
            btail(3, "3.000", "0.000"),
        ], ftail_detail_count=4)
        report, _, _ = self.audit(b)
        b1, b2 = report["batches"]
        self.assertEqual(b1["status"], "rejected")
        self.assertIn("bad_seq",
                      {d["code"] for d in b1["diagnostics"]})
        self.assertEqual(b2["status"], "accepted")
        # only B2's single detail line is deliverable
        self.assertEqual(report["accepted_detail_count"], 1)
        self.assertEqual(len(report["accepted_detail_line_numbers"]), 1)

    def test_detail_batch_id_mismatch(self):
        b = self.build([
            fhead(), bhead("B1"),
            detail(1, batch_id="OTHER"),
            btail(1, "1.000", "0.000"), ftail(),
        ])
        report, _, _ = self.audit(b)
        self.assertEqual(report["batches"][0]["status"], "rejected")
        self.assertIn("batch_id_mismatch",
                      {d["code"] for d in report["batches"][0]["diagnostics"]})

    def test_illegal_category_and_quantity(self):
        b = self.build([
            fhead(), bhead(),
            detail(1, category="ZZZ", quantity="nope"),
            btail(1, "0.000", "0.000"), ftail(),
        ])
        report, _, _ = self.audit(b)
        codes = {d["code"] for d in report["batches"][0]["diagnostics"]}
        self.assertIn("invalid_category", codes)
        self.assertIn("invalid_quantity", codes)
        # line still counts as a received detail position
        self.assertEqual(report["batches"][0]["detail_count"]["actual"], 1)

    def test_btail_count_mismatch_cannot_use_file_totals(self):
        b = self.build([
            fhead(), bhead(), detail(1),
            btail(2, "1.000", "0.000"),            # declares 2, has 1
            ftail(),
        ])
        report, _, _ = self.audit(b)
        batch = report["batches"][0]
        self.assertEqual(batch["status"], "rejected")
        self.assertFalse(batch["detail_count"]["ok"])
        self.assertIn("btail_count_mismatch",
                      {d["code"] for d in batch["diagnostics"]})

    def test_btail_total_mismatch(self):
        b = self.build([
            fhead(), bhead(), detail(1, quantity="1.000"),
            btail(1, "9.000", "0.000"), ftail(),
        ])
        report, _, _ = self.audit(b)
        batch = report["batches"][0]
        self.assertEqual(batch["quantity_check"]["status"], "mismatch")
        aaa = batch["category_totals"][0]
        self.assertEqual(aaa["declared_milli"], 9000)
        self.assertEqual(aaa["actual_milli"], 1000)
        self.assertFalse(aaa["ok"])
        # categories not seen are checked against zero explicitly
        bbb = batch["category_totals"][1]
        self.assertEqual(bbb["actual_milli"], 0)
        self.assertTrue(bbb["ok"])

    def test_undecidable_quantity_is_not_treated_as_zero(self):
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.rec("BHEAD", **{"BATCH-ID": "B1"})
        b.rec("DETAIL", **{"BATCH-ID": "B1", "SEQ": "1",
                           "CATEGORY": "AAA", "QTY": "1.000"})
        b.quar()
        b.rec("BTAIL", **{"BATCH-ID": "B1", "DETAIL-COUNT": "2",
                          "TOTAL-AAA": "1.000", "TOTAL-BBB": "0.000"})
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "1",
                          "DETAIL-COUNT": "2"})
        report, _, _ = self.audit(b)
        batch = report["batches"][0]
        self.assertEqual(batch["quantity_check"]["status"], "undecidable")
        for total in batch["category_totals"]:
            self.assertTrue(total["undecidable"])
            self.assertIsNone(total["actual_milli"])
            self.assertIsNone(total["ok"])


    def test_quarantine_does_not_advance_seq_expectation(self):
        # A quarantined line has no readable seq. The DETAIL after it keeps
        # the normal per-batch seq numbering (here seq=1) and must not be
        # hit with an additional bad_seq diagnostic.
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.rec("BHEAD", **{"BATCH-ID": "B1"})
        b.quar()
        b.rec("DETAIL", **{"BATCH-ID": "B1", "SEQ": "1",
                           "CATEGORY": "AAA", "QTY": "1.000"})
        b.rec("BTAIL", **{"BATCH-ID": "B1", "DETAIL-COUNT": "2",
                          "TOTAL-AAA": "1.000", "TOTAL-BBB": "0.000"})
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "1",
                          "DETAIL-COUNT": "2"})
        report, _, _ = self.audit(b)
        batch = report["batches"][0]
        codes = {d["code"] for d in batch["diagnostics"]}
        self.assertEqual(codes, {"quarantined_detail"})
        self.assertEqual(batch["detail_count"]["actual"], 2)
        self.assertEqual(batch["quantity_check"]["status"], "undecidable")


class TestFileLevelRejection(EngineTestCase):
    def test_duplicate_batch_id_rejects_file(self):
        b = self.build([
            fhead(),
            bhead("DUP"), detail(1, "DUP"),
            btail(1, batch_id="DUP"),
            bhead("DUP"), detail(1, "DUP"),
            btail(1, batch_id="DUP"),
            ftail(batch_count=2, detail_count=2),
        ])
        report, out, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertEqual(report["accepted_detail_line_numbers"], [])
        # still zero bytes of accepted details delivered
        self.assertEqual(
            (out / "accepted_details.jsonl").read_text(encoding="utf-8"), "")
        self.assertIn("duplicate_batch_id",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_bhead_inside_open_batch_is_fatal_no_recovery(self):
        b = self.build([
            fhead(), bhead("B1"), detail(1, "B1"),
            bhead("B2"),                            # B1 never closed
            detail(1, "B2"),
            btail(1, batch_id="B2"),
            ftail(batch_count=2, detail_count=2),
        ])
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("unterminated_batch",
                      {d["code"] for d in report["file_diagnostics"]})
        self.assertEqual(report["accepted_detail_line_numbers"], [])

    def test_detail_outside_batch(self):
        b = self.build([fhead(), detail(1), ftail(detail_count=0)])
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("detail_outside_batch",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_orphan_quarantine_outside_batch(self):
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.quar()
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "0",
                          "DETAIL-COUNT": "0"})
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("orphan_quarantine",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_unknown_record_type(self):
        b = self.build([fhead(),
                        ("WAT", {"X": "y"}),
                        ftail(batch_count=0, detail_count=0)])
        report, _, _ = self.audit(b)
        self.assertIn("unknown_record_type",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_missing_ftail(self):
        b = self.build([fhead(), bhead(), btail(0, "0.000", "0.000")])
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("missing_ftail",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_missing_fhead(self):
        b = self.build([bhead(), btail(0, "0.000", "0.000"),
                        ftail(batch_count=1, detail_count=0)])
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("unexpected_before_fhead",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_data_after_ftail(self):
        b = self.build([fhead(),
                        ftail(batch_count=0, detail_count=0),
                        detail(1)])
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertIn("data_after_ftail",
                      {d["code"] for d in report["file_diagnostics"]})

    def test_file_id_mismatch(self):
        b = self.build([fhead("F1"),
                        ftail(file_id="F2", batch_count=0, detail_count=0)])
        report, _, _ = self.audit(b)
        self.assertIn("file_id_mismatch",
                      {d["code"] for d in report["file_diagnostics"]})
        self.assertFalse(report["file_checks"]["file_id"]["ok"])

    def test_ftail_counts_include_rejected_batch_and_quarantine(self):
        # FTAIL deliberately under-reports; rejected batches' details and
        # quarantine positions must still be counted by the engine.
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.rec("BHEAD", **{"BATCH-ID": "B1"})
        b.quar()                                  # 1 position, batch rejected
        b.rec("BTAIL", **{"BATCH-ID": "B1", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "0.000", "TOTAL-BBB": "0.000"})
        b.rec("BHEAD", **{"BATCH-ID": "B2"})
        b.rec("DETAIL", **{"BATCH-ID": "B2", "SEQ": "1",
                           "CATEGORY": "AAA", "QTY": "4.000"})
        b.rec("BTAIL", **{"BATCH-ID": "B2", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "4.000", "TOTAL-BBB": "0.000"})
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "2",
                          "DETAIL-COUNT": "1"})     # lies: 2 positions exist
        report, _, _ = self.audit(b)
        self.assertTrue(report["file_rejected"])
        self.assertEqual(
            report["file_checks"]["detail_count"]["actual"], 2)
        self.assertIn("ftail_detail_count_mismatch",
                      {d["code"] for d in report["file_diagnostics"]})
        # the late file-level fault revokes the previously-good batch
        self.assertEqual(report["accepted_detail_line_numbers"], [])

    def test_correct_ftail_counts_pass_with_rejected_batch(self):
        b = Builder()
        b.rec("FHEAD", **{"FILE-ID": "F1"})
        b.rec("BHEAD", **{"BATCH-ID": "B1"})
        b.quar()
        b.rec("BTAIL", **{"BATCH-ID": "B1", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "0.000", "TOTAL-BBB": "0.000"})
        b.rec("BHEAD", **{"BATCH-ID": "B2"})
        b.rec("DETAIL", **{"BATCH-ID": "B2", "SEQ": "1",
                           "CATEGORY": "AAA", "QTY": "4.000"})
        b.rec("BTAIL", **{"BATCH-ID": "B2", "DETAIL-COUNT": "1",
                          "TOTAL-AAA": "4.000", "TOTAL-BBB": "0.000"})
        b.rec("FTAIL", **{"FILE-ID": "F1", "BATCH-COUNT": "2",
                          "DETAIL-COUNT": "2"})
        report, _, _ = self.audit(b)
        self.assertFalse(report["file_rejected"])
        self.assertEqual(report["conclusion"], BUSINESS_REJECTED)
        # good second batch remains deliverable
        self.assertEqual(len(report["accepted_detail_line_numbers"]), 1)


class TestDeliverySafety(EngineTestCase):
    def test_existing_output_dir_is_not_overwritten(self):
        b = self.build([fhead(), ftail(batch_count=0, detail_count=0)])
        rp, qp, total = b.write(self.dir)
        out = self.dir / "delivery"
        out.mkdir()
        (out / "keep.txt").write_text("important")
        with self.assertRaises(AuditInputError):
            run_audit(rp, qp, config=self.cfg, expected_source=SRC,
                      expected_lines=total, output_dir=out,
                      temp_dir=self.dir / "staging")
        self.assertEqual((out / "keep.txt").read_text(), "important")

    def test_staging_subdirectory_is_cleaned(self):
        b = self.build([fhead(), ftail(batch_count=0, detail_count=0)])
        rp, qp, total = b.write(self.dir)
        tmp = self.dir / "staging"
        tmp.mkdir()
        marker = tmp / "caller-file.txt"
        marker.write_text("mine")
        run_audit(rp, qp, config=self.cfg, expected_source=SRC,
                  expected_lines=total, output_dir=self.dir / "delivery",
                  temp_dir=tmp)
        # caller's own file survives; only the auditor's subdir is removed
        self.assertTrue(marker.exists())
        self.assertEqual([p.name for p in tmp.iterdir()],
                         ["caller-file.txt"])

    def test_input_gap_leaves_no_delivery(self):
        b = self.build([fhead(), ftail(batch_count=0, detail_count=0)])
        rp, qp, _ = b.write(self.dir)
        out = self.dir / "delivery"
        with self.assertRaises(AuditInputError):
            run_audit(rp, qp, config=self.cfg, expected_source=SRC,
                      expected_lines=99, output_dir=out,
                      temp_dir=self.dir / "staging")
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
