"""Streaming evidence merge: exact 1..N coverage, ordering, source checks."""

import json
import tempfile
import unittest
from pathlib import Path

from copybook.audit import AuditInputError
from copybook.audit.evidence import iter_evidence

SRC = "/landing/feed.dat"


def _record(line, start, end, source=SRC, fields=None, extra_meta=None):
    meta = {"source_file": source, "line_number": line,
            "byte_start": start, "byte_end": end,
            "layout": "X", "layout_version": "1"}
    if extra_meta:
        meta.update(extra_meta)
    return {"meta": meta, "fields": fields or {"REC-TYPE": "DETAIL"}}


def _quar(line, start, end, source=SRC, reason="decode", raw_hex="ff"):
    return {"source_file": source, "line_number": line,
            "byte_start": start, "byte_end": end,
            "reason": reason, "error": "bad", "raw_hex": raw_hex}


def _write(d, name, rows):
    p = Path(d) / name
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return p


class MergeTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def merge(self, recs, quars, *, lines=None, source=SRC):
        rp = _write(self.dir, "r.jsonl", recs)
        qp = _write(self.dir, "q.jsonl", quars)
        if lines is None:
            lines = len(recs) + len(quars)
        return list(iter_evidence(rp, qp, expected_source=source,
                                  expected_lines=lines))


class TestHappyMerge(MergeTestCase):
    def test_quarantine_hole_does_not_renumber(self):
        # physical lines 1..4; line 3 is quarantined, good lines keep 1,2,4
        recs = [_record(1, 0, 10), _record(2, 11, 21), _record(4, 33, 43)]
        quars = [_quar(3, 22, 32)]
        ev = self.merge(recs, quars, lines=4)
        self.assertEqual([(e.kind, e.line_number) for e in ev],
                         [("record", 1), ("record", 2),
                          ("quarantine", 3), ("record", 4)])
        self.assertIsNone(ev[2].fields)
        self.assertEqual(ev[2].raw["raw_hex"], "ff")

    def test_empty_quarantine_index(self):
        recs = [_record(1, 0, 10), _record(2, 11, 21)]
        ev = self.merge(recs, [], lines=2)
        self.assertEqual([e.line_number for e in ev], [1, 2])


class TestCoverageFailures(MergeTestCase):
    def test_gap_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10), _record(3, 22, 32)], [], lines=3)

    def test_duplicate_line_in_same_file_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10), _record(1, 11, 21)], [], lines=2)

    def test_line_in_both_inputs_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10)],
                       [_quar(1, 0, 10)], lines=1)

    def test_out_of_range_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(0, 0, 10)], [], lines=1)
        with self.assertRaises(AuditInputError):
            self.merge([_record(2, 0, 10)], [], lines=1)

    def test_expected_count_mismatch_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10)], [], lines=2)

    def test_unsorted_input_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(2, 11, 21), _record(1, 0, 10)], [], lines=2)


class TestEvidenceIntegrity(MergeTestCase):
    def test_source_mismatch_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10, source="/other.dat")], [],
                       lines=1)

    def test_backwards_byte_ranges_fails(self):
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 10, 5)], [], lines=1)
        with self.assertRaises(AuditInputError):
            self.merge([_record(1, 0, 10), _record(2, 9, 19)], [], lines=2)

    def test_adjacent_ranges_are_allowed(self):
        ev = self.merge([_record(1, 0, 10), _record(2, 10, 20)], [],
                        lines=2)
        self.assertEqual(len(ev), 2)

    def test_raw_hex_in_normal_record_rejected(self):
        row = _record(1, 0, 10)
        row["raw_hex"] = "deadbeef"
        with self.assertRaises(AuditInputError):
            self.merge([row], [], lines=1)

    def test_fields_in_quarantine_rejected(self):
        row = _quar(1, 0, 10)
        row["fields"] = {"REC-TYPE": "DETAIL"}
        with self.assertRaises(AuditInputError):
            self.merge([], [row], lines=1)

    def test_malformed_json_fails(self):
        rp = Path(self.dir) / "r.jsonl"
        qp = Path(self.dir) / "q.jsonl"
        rp.write_text("{broken", encoding="utf-8")
        qp.write_text("", encoding="utf-8")
        with self.assertRaises(AuditInputError):
            list(iter_evidence(rp, qp, expected_source=SRC,
                               expected_lines=1))

    def test_bad_expected_arguments(self):
        rp = _write(self.dir, "r.jsonl", [])
        qp = _write(self.dir, "q.jsonl", [])
        with self.assertRaises(AuditInputError):
            list(iter_evidence(rp, qp, expected_source=SRC,
                               expected_lines=0))
        with self.assertRaises(AuditInputError):
            list(iter_evidence(rp, qp, expected_source="",
                               expected_lines=1))


if __name__ == "__main__":
    unittest.main()
