"""Additional contract edge cases required by the rewrite specification.

Covers duplicate-path overlap semantics, a conflict only revealed at the
last edit, write-path failure handling, nested duplicate JSON members,
exact-capacity text, COMP-3 negative zero, and evidence replay.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from copybook.rewrite import (RewriteConflictError, RewriteIOError,
                              RewriteOrderError, rewrite_original)
from tests.rewrite.fixtures import build_demo_file, sha256_hex, write_layouts
from tests.rewrite.test_engine import (DemoFixture, fill_record_digests,
                                       make_order, record_entry)


class DuplicatePathAndLateConflictTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _two_flag_edits(self, values):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        edits = [
            {"path": ["FLAG"], "expected_hex": rec[43:44].hex(),
             "value": values[0]},
            {"path": ["FLAG"], "expected_hex": rec[43:44].hex(),
             "value": values[1]},
        ]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", edits, spans=self.fx.spans)])
        return fill_record_digests(self.fx.source, order)

    def test_duplicate_path_identical_demand_is_legal(self):
        evidence = self.fx.run(self._two_flag_edits(("9", "9")))
        # two requested fields, but the single byte is covered once
        self.assertEqual(evidence["counts"]["requested_fields"], 2)
        self.assertEqual(evidence["counts"]["covered_bytes"], 1)
        self.assertEqual(evidence["counts"]["changed_bytes"], 1)
        out = self.fx.output_bytes()
        self.assertEqual(out[self.fx.spans["A"][0] + 43], 0xF9)

    def test_duplicate_path_conflicting_demand_rejects(self):
        with self.assertRaises(RewriteConflictError):
            self.fx.run(self._two_flag_edits(("9", "8")))
        self.assertFalse(self.fx.out_dir.exists())

    def test_conflict_only_in_the_last_edit_still_rejects_all(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        edits = [
            {"path": ["ITEM-GROUP", 0, "ITEM-AMOUNT"],
             "expected_hex": rec[24:28].hex(), "value": "405.04"},
            {"path": ["ITEM-GROUP", 0, "ITEM-NOTE"],
             "expected_hex": rec[24:28].hex(), "value": "ZZZZ"},
        ]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", edits, spans=self.fx.spans)])
        fill_record_digests(self.fx.source, order)
        with self.assertRaises(RewriteConflictError):
            self.fx.run(order)
        self.assertFalse(self.fx.out_dir.exists())
        # source untouched after the refusal
        self.assertEqual(self.fx.source_path.read_bytes(), self.fx.source)


class WriteFailureTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_failed_write_removes_half_finished_delivery(self):
        order = make_order(source=self.fx.source, spans=self.fx.spans,
                           records=[])
        order_path = self.fx.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        # Make the output location impossible: its parent is a regular file.
        blocker = self.fx.tmp / "blocker"
        blocker.write_text("not a directory")
        with self.assertRaises(RewriteIOError):
            rewrite_original(
                order_path, source_path=self.fx.source_path,
                output_dir=blocker / "delivery",
                temp_dir=self.fx.staging,
                copybook_base=self.fx.copybooks["ORDA.cpy"].parent)
        self.assertFalse((blocker / "delivery").exists())
        # the staging area this job created is cleaned up
        self.assertEqual(
            [p for p in self.fx.staging.iterdir() if p.is_dir()], [])


class NestedDuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_duplicate_edit_member_rejected(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        text = (
            "{"
            f'"source_sha256": "{sha256_hex(self.fx.source)}", '
            '"layouts": {"A": {"copybook": "ORDA.cpy", "name": "ORD-A", '
            '"version": "1", "codec": "cp037"}}, '
            '"records": [{"byte_start": %d, "byte_end": %d, "layout": "A", '
            '"record_sha256": "%s", '
            '"edits": [{"path": ["FLAG"], "expected_hex": "%s", '
            '"value": "9", "value": "8"}]}]}'
            % (a0, a1, sha256_hex(rec), rec[43:44].hex()))
        order_path = self.fx.tmp / "dup.json"
        order_path.write_text(text, encoding="utf-8")
        with self.assertRaises(RewriteOrderError):
            rewrite_original(
                order_path, source_path=self.fx.source_path,
                output_dir=self.fx.tmp / "out",
                temp_dir=self.fx.staging,
                copybook_base=self.fx.copybooks["ORDA.cpy"].parent)
        self.assertFalse((self.fx.tmp / "out").exists())


class ExactCapacityAndComp3ZeroTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_text_at_exact_capacity_then_overflow(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        ok = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", [{
                "path": ["ORDER-ID"], "expected_hex": rec[0:8].hex(),
                "value": "12345678"}], spans=self.fx.spans)])
        fill_record_digests(self.fx.source, ok)
        self.fx.run(ok)
        self.assertEqual(
            self.fx.output_bytes()[a0:a0 + 8],
            "12345678".encode("cp037"))
        self._td.cleanup()
        self.setUp()
        bad = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", [{
                "path": ["ORDER-ID"], "expected_hex": rec[0:8].hex(),
                "value": "123456789"}], spans=self.fx.spans)])
        fill_record_digests(self.fx.source, bad)
        from copybook.rewrite import RewriteValueError
        with self.assertRaises(RewriteValueError):
            self.fx.run(bad)

    def test_comp3_negative_zero_written_as_positive_zero(self):
        from copybook.parser import parse_copybook
        from copybook.rewrite.encoder import encode_value
        from tests.rewrite.fixtures import LAYOUT_A
        layout = parse_copybook(LAYOUT_A, codec="cp037")
        target = layout.find("TOTAL")  # signed COMP-3
        got = encode_value("-0.00", target, "cp037")
        self.assertEqual(got[-1] & 0x0F, 0x0C)   # positive C, not D
        self.assertEqual(got, bytes(4) + b"\x0c")


class UnlistedRecordPreservationTests(unittest.TestCase):
    def test_unlisted_records_and_inter_record_bytes_are_copied(self):
        with TemporaryDirectory() as td:
            fx = DemoFixture(Path(td))
            # The demo file already contains preamble/gap/trailer bytes.
            # List only record A; record B and every surrounding byte must
            # survive untouched even though layout B exists in the catalog.
            a0, a1 = fx.spans["A"]
            rec = fx.source[a0:a1]
            order = make_order(
                source=fx.source, spans=fx.spans,
                records=[record_entry("A", [{
                    "path": ["FLAG"],
                    "expected_hex": rec[43:44].hex(),
                    "value": "9"}], spans=fx.spans)])
            fill_record_digests(fx.source, order)
            fx.run(order)
            out = fx.output_bytes()
            # the single change is inside record A only
            b0, b1 = fx.spans["B"]
            self.assertEqual(out[b0:b1], fx.source[b0:b1])
            self.assertEqual(out[:a0], fx.source[:a0])
            self.assertEqual(out[a1:b0], fx.source[a1:b0])
            self.assertEqual(out[b1:], fx.source[b1:])
            # and the one changed byte is exactly where expected
            self.assertEqual(
                [i for i in range(len(out)) if out[i] != fx.source[i]],
                [a0 + 43])


class EvidenceReplayTests(unittest.TestCase):
    """A reader can reproduce the product using only evidence + original."""

    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_replay_change_intervals_reproduces_product(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", [{
                "path": ["FLAG"], "expected_hex": rec[43:44].hex(),
                "value": "9"}], spans=self.fx.spans)])
        fill_record_digests(self.fx.source, order)
        evidence = self.fx.run(order)
        replay = bytearray(self.fx.source)
        for iv in evidence["change_intervals"]:
            replay[iv["byte_start"]:iv["byte_end"]] = bytes.fromhex(
                iv["after_hex"])
        self.assertEqual(bytes(replay), self.fx.output_bytes())
        # the evidence stores no full original copy
        serialized = json.dumps(evidence)
        self.assertNotIn(self.fx.source.hex(), serialized)


if __name__ == "__main__":
    unittest.main()
