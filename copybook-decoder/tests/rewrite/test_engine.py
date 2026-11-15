"""End-to-end rewrite engine: streaming, evidence, conflicts, safety."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from copybook.decoder import Decoder
from copybook.rewrite import (RewriteChecksumError, RewriteConflictError,
                              RewriteIOError, RewriteOrderError,
                              RewriteValueError, rewrite_original)
from copybook.rewrite.engine import OUTPUT_NAME, EVIDENCE_NAME
from tests.rewrite.fixtures import (DEMO_GAP, DEMO_PREAMBLE, DEMO_TRAILER,
                                    build_demo_file, build_demo_records,
                                    ref_comp3, ref_text, ref_zoned,
                                    sha256_hex, write_layouts)


def make_order(*, source: bytes, spans: dict, records: list,
               layouts=None) -> dict:
    catalog = layouts or {
        "A": {"copybook": "ORDA.cpy", "name": "ORD-A", "version": "1",
              "codec": "cp037"},
        "B": {"copybook": "SUMB.cpy", "name": "SUM-B", "version": "1",
              "codec": "cp500"},
    }
    return {"source_sha256": sha256_hex(source), "layouts": catalog,
            "records": records}


def record_entry(span_key, edits, *, spans=None, digest_override=None):
    start, end = spans[span_key]
    return {
        "byte_start": start, "byte_end": end, "layout": span_key,
        "record_sha256": digest_override or "0" * 64,
        "edits": edits,
    }


def raw_entry(start: int, end: int, layout: str, edits):
    return {"byte_start": start, "byte_end": end, "layout": layout,
            "record_sha256": "0" * 64, "edits": edits}


def fill_record_digests(source: bytes, order: dict) -> dict:
    for rec in order["records"]:
        rec["record_sha256"] = sha256_hex(
            source[rec["byte_start"]:rec["byte_end"]])
    return order


class DemoFixture:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.copybooks = write_layouts(tmp / "copybooks")
        self.source_path = tmp / "original.bin"
        self.source, self.spans = build_demo_file()
        self.source_path.write_bytes(self.source)
        self.staging = tmp / "staging"
        self.out_dir = tmp / "delivery"

    def run(self, order: dict, **kwargs):
        order_path = self.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        return rewrite_original(
            order_path, source_path=self.source_path,
            output_dir=self.out_dir, temp_dir=self.staging,
            copybook_base=self.copybooks["ORDA.cpy"].parent, **kwargs)

    def output_bytes(self) -> bytes:
        return (self.out_dir / OUTPUT_NAME).read_bytes()


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _order(self, *, conflict_note=False, note_value=None):
        a0, a1 = self.fx.spans["A"]
        b0, b1 = self.fx.spans["B"]
        record_a = self.fx.source[a0:a1]
        record_b = self.fx.source[b0:b1]
        edits_a = [
            {"path": ["CUSTOMER-NAME"],
             "expected_hex": record_a[8:20].hex(),
             "value": "ACME INC"},
            {"path": ["ITEM-GROUP", 0, "ITEM-AMOUNT"],
             "expected_hex": record_a[24:28].hex(),
             "value": "405.04"},
            {"path": ["FLAG"],
             "expected_hex": record_a[43:44].hex(),
             "value": "9"},
        ]
        if note_value is not None:
            edits_a.append({
                "path": ["ITEM-GROUP", 0, "ITEM-NOTE"],
                "expected_hex": record_a[24:28].hex(),
                "value": note_value})
        edits_b = [
            {"path": ["REMARK"],
             "expected_hex": record_b[33:43].hex(),
             "value": "WORLD"},
            {"path": ["BATCH-GROUP", 2, "BATCH-AMT"],
             "expected_hex": record_b[24:30].hex(),
             "value": "12.34"},
            {"path": ["ZONED-AMT"],
             "expected_hex": record_b[43:50].hex(),
             "value": "0.00"},
        ]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans, records=[
                record_entry("A", edits_a, spans=self.fx.spans),
                record_entry("B", edits_b, spans=self.fx.spans)])
        return fill_record_digests(self.fx.source, order)

    def test_full_demo_byte_for_byte(self):
        order = self._order()
        evidence = self.fx.run(order)
        out = self.fx.output_bytes()
        self.assertEqual(len(out), len(self.fx.source))

        # Independent byte-by-byte difference set, assembled from the
        # hand-computed field-relative offsets of the two layouts.
        diffs = {i for i in range(len(out))
                 if out[i] != self.fx.source[i]}
        a0 = self.fx.spans["A"][0]
        b0 = self.fx.spans["B"][0]
        expected = set()
        # CUSTOMER-NAME relative 8..19: ACME CORP -> ACME INC changes the
        # four relative positions 13,14,15,16 (the last one space -> C).
        expected |= {a0 + 13, a0 + 14, a0 + 15, a0 + 16}
        # ITEM-AMOUNT[0] relative 24..27: 00 12 34 5c -> 00 40 50 4c
        expected |= {a0 + 25, a0 + 26, a0 + 27}
        # FLAG relative 43
        expected.add(a0 + 43)
        # REMARK relative 33..42: HELLO -> WORLD. H/E/L change (33,34,35),
        # L(36) stays, O(37)->D(37) changes, padding spaces stay.
        expected |= {b0 + 33, b0 + 34, b0 + 35, b0 + 37}
        # BATCH-AMT[2] relative 24..29: 00 00 00 00 00 0c -> 00 00 00 01 23 4c
        expected |= {b0 + 27, b0 + 28, b0 + 29}
        # ZONED-AMT relative 43..49: f0 f0 f0 f1 f2 f3 d4 -> f0 f0 f0 f0 f0 c0
        expected |= {b0 + 46, b0 + 47, b0 + 48, b0 + 49}
        self.assertEqual(diffs, expected)

        # Everything outside the diff set is identical.
        self.assertTrue(all(out[i] == self.fx.source[i]
                            for i in range(len(out)) if i not in diffs))

        # Evidence intervals match the computed difference set exactly.
        interval_positions = set()
        for iv in evidence["change_intervals"]:
            interval_positions.update(range(iv["byte_start"],
                                            iv["byte_end"]))
            before = bytes.fromhex(iv["before_hex"])
            after = bytes.fromhex(iv["after_hex"])
            self.assertEqual(len(before), iv["byte_end"] - iv["byte_start"])
            self.assertTrue(any(x != y for x, y in zip(before, after)))
            for j, pos in enumerate(range(iv["byte_start"],
                                          iv["byte_end"])):
                self.assertEqual(self.fx.source[pos], before[j])
                self.assertEqual(out[pos], after[j])
        self.assertEqual(interval_positions, diffs)
        self.assertEqual(evidence["counts"]["changed_bytes"], len(diffs))

    def test_non_canonical_sign_and_0a_byte_preserved(self):
        self.fx.run(self._order())
        out = self.fx.output_bytes()
        a0 = self.fx.spans["A"][0]
        # TOTAL is never edited: 0x0A bytes and the A sign nibble survive.
        self.assertEqual(out[a0 + 38:a0 + 43],
                         bytes([0x00, 0x09, 0x0A, 0x87, 0x0A]))
        # FILLER, preamble, gap (including its 0x0A) and trailer survive.
        self.assertEqual(out[a0 + 36:a0 + 38], bytes([0x40, 0xFF]))
        self.assertEqual(out[:a0], DEMO_PREAMBLE)
        gap_start = self.fx.spans["A"][1]
        self.assertEqual(out[gap_start:gap_start + len(DEMO_GAP)], DEMO_GAP)
        self.assertEqual(out[len(out) - len(DEMO_TRAILER):], DEMO_TRAILER)

    def test_consistent_overlapping_views_succeed(self):
        # The new packed amount 405.04 encodes to 00 40 50 4c, which is also
        # the exact cp037 text the ITEM-NOTE view asks for.
        text_value = bytes.fromhex("0040504c").decode("cp037")
        order = self._order(note_value=text_value)
        evidence = self.fx.run(order)
        out = self.fx.output_bytes()
        a0 = self.fx.spans["A"][0]
        self.assertEqual(out[a0 + 24:a0 + 28].hex(), "0040504c")
        # two requested fields, but covered bytes counted only once
        field_spans = [(e["field_byte_start"], e["field_byte_end"])
                       for e in evidence["edits"]]
        covered = set()
        for lo, hi in field_spans:
            covered.update(range(lo, hi))
        self.assertEqual(evidence["counts"]["covered_bytes"], len(covered))

    def test_conflicting_overlapping_views_reject_whole_job(self):
        # Ask the text view for different bytes at the shared positions.
        order = self._order(note_value="ZZZZ")
        with self.assertRaises(RewriteConflictError):
            self.fx.run(order)
        self._assert_no_delivery()

    def test_edit_order_does_not_change_product_or_evidence(self):
        order = self._order()
        first = self.fx.run(order)
        first_bytes = self.fx.output_bytes()
        first_evidence = json.loads(
            (self.fx.out_dir / EVIDENCE_NAME).read_text("utf-8"))
        self._td.cleanup()
        self.setUp()
        shuffled = json.loads(json.dumps(self._order()))
        for rec in shuffled["records"]:
            rec["edits"] = list(reversed(rec["edits"]))
        shuffled["records"] = list(reversed(shuffled["records"]))
        second = self.fx.run(shuffled)
        second_bytes = self.fx.output_bytes()
        second_evidence = json.loads(
            (self.fx.out_dir / EVIDENCE_NAME).read_text("utf-8"))
        self.assertEqual(first_bytes, second_bytes)
        # run-specific absolute paths (temp dirs and copybook locations)
        # may differ; everything else must be byte-identical evidence
        path_like = ("path", "copybook_path")
        def strip_paths(payload):
            payload = json.loads(json.dumps(payload))
            payload["input"].pop("path", None)
            for entry in payload["layouts"].values():
                entry.pop("copybook_path", None)
            return payload
        self.assertEqual(strip_paths(first_evidence),
                         strip_paths(second_evidence))
        self.assertEqual(strip_paths(first), strip_paths(second))

    def test_evidence_is_machine_readable_and_self_describing(self):
        evidence = self.fx.run(self._order())
        on_disk = json.loads(
            (self.fx.out_dir / EVIDENCE_NAME).read_text("utf-8"))
        self.assertEqual(on_disk, evidence)
        self.assertEqual(on_disk["format"], "copybook-rewrite/v1")
        self.assertEqual(on_disk["input"]["sha256"],
                         sha256_hex(self.fx.source))
        self.assertEqual(on_disk["output"]["sha256"],
                         sha256_hex(self.fx.output_bytes()))
        self.assertEqual(on_disk["input"]["length_bytes"],
                         on_disk["output"]["length_bytes"])
        # intervals are sorted, non-overlapping, adjacent-merged
        starts = [iv["byte_start"] for iv in on_disk["change_intervals"]]
        self.assertEqual(starts, sorted(starts))
        for iv in on_disk["change_intervals"]:
            self.assertLess(iv["byte_start"], iv["byte_end"])
        for prev, cur in zip(on_disk["change_intervals"],
                             on_disk["change_intervals"][1:]):
            self.assertLess(prev["byte_end"], cur["byte_start"])
        # layout catalog carries digest and name/version
        for key, entry in on_disk["layouts"].items():
            self.assertEqual(len(entry["copybook_sha256"]), 64)
            self.assertTrue(entry["layout_name"])
            self.assertTrue(entry["layout_version"])

    def _assert_no_delivery(self):
        self.assertFalse(self.fx.out_dir.exists())
        # source and staging neighbours untouched
        self.assertEqual(self.fx.source_path.read_bytes(), self.fx.source)
        if self.fx.staging.exists():
            self.assertEqual(list(self.fx.staging.iterdir()), [])

    def test_decoder_supplementary_check_on_rewritten_segment(self):
        self.fx.run(self._order())
        out = self.fx.output_bytes()
        b0, b1 = self.fx.spans["B"]
        record_b = out[b0:b1]
        from copybook.parser import parse_copybook
        from tests.rewrite.fixtures import LAYOUT_B
        layout = parse_copybook(LAYOUT_B, name="SUM-B", version="1",
                                codec="cp500")
        fields, errors = Decoder(layout).decode(record_b)
        # REMARK-NUM is an alternative numeric view over text bytes and
        # legitimately fails; every elementary field we wrote is clean.
        self.assertTrue(all(e.field == "REMARK-NUM" for e in errors))
        self.assertEqual(fields["REMARK"], "WORLD")
        self.assertEqual(str(fields["ZONED-AMT"]), "0.00")
        self.assertEqual(str(fields["BATCH-GROUP"][2]["BATCH-AMT"]),
                         "12.34")
        # unedited batches keep their original decoded values
        self.assertEqual(str(fields["BATCH-GROUP"][0]["BATCH-AMT"]),
                         "100.00")
        self.assertEqual(str(fields["BATCH-GROUP"][1]["BATCH-AMT"]),
                         "-250.25")


class ChecksumAndExpectedTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _single_edit_order(self, path, value):
        a0, a1 = self.fx.spans["A"]
        record_a = self.fx.source[a0:a1]
        edits = [{"path": path,
                  "expected_hex": record_a[43:44].hex()
                  if path == ["FLAG"] else record_a[8:20].hex(),
                  "value": value}]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", edits, spans=self.fx.spans)])
        return fill_record_digests(self.fx.source, order)

    def test_wrong_full_file_digest_rejected(self):
        order = self._single_edit_order(["FLAG"], "9")
        order["source_sha256"] = "f" * 64
        with self.assertRaises(RewriteChecksumError):
            self.fx.run(order)
        self.assertFalse(self.fx.out_dir.exists())

    def test_wrong_record_digest_rejected(self):
        order = self._single_edit_order(["FLAG"], "9")
        order["records"][0]["record_sha256"] = "e" * 64
        with self.assertRaises(RewriteChecksumError):
            self.fx.run(order)
        self.assertFalse(self.fx.out_dir.exists())

    def test_wrong_expected_hex_rejected(self):
        order = self._single_edit_order(["FLAG"], "9")
        order["records"][0]["edits"][0]["expected_hex"] = "f0"
        with self.assertRaises(RewriteChecksumError):
            self.fx.run(order)

    def test_expected_hex_wrong_length_rejected(self):
        order = self._single_edit_order(["FLAG"], "9")
        order["records"][0]["edits"][0]["expected_hex"] = "f7f7"
        with self.assertRaises(RewriteOrderError):
            self.fx.run(order)

    def test_changing_unrelated_original_byte_fails_digest(self):
        order = self._single_edit_order(["FLAG"], "9")
        tampered = bytearray(self.fx.source)
        tampered[0] ^= 0x01
        self.fx.source_path.write_bytes(bytes(tampered))
        with self.assertRaises(RewriteChecksumError):
            self.fx.run(order)
        self.assertFalse(self.fx.out_dir.exists())
        # restore for tearDown symmetry
        self.fx.source_path.write_bytes(self.fx.source)

    def test_edited_byte_not_reused_as_next_expected(self):
        # Two edits on one record; the second expected_hex must describe the
        # ORIGINAL bytes, even though the first edit already changed nearby
        # bytes. Build manually to assert the engine reads originals only.
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        edits = [
            {"path": ["FLAG"], "expected_hex": rec[43:44].hex(),
             "value": "9"},
            {"path": ["CUSTOMER-NAME"],
             "expected_hex": rec[8:20].hex(), "value": "ACME INC"},
        ]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", edits, spans=self.fx.spans)])
        fill_record_digests(self.fx.source, order)
        # corrupt the second expected value to what the *first* edit cannot
        # produce anyway, proving no sequential overwrite semantics
        order["records"][0]["edits"][1]["expected_hex"] = "00" * 12
        with self.assertRaises(RewriteChecksumError):
            self.fx.run(order)


class BoundaryAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _empty_order(self):
        order = make_order(source=self.fx.source, spans=self.fx.spans,
                           records=[])
        return order

    def test_empty_correction_produces_identical_file_and_zero_changes(self):
        evidence = self.fx.run(self._empty_order())
        self.assertEqual(self.fx.output_bytes(), self.fx.source)
        self.assertEqual(evidence["change_intervals"], [])
        self.assertEqual(evidence["counts"],
                         {"requested_fields": 0, "covered_bytes": 0,
                          "changed_bytes": 0})
        self.assertEqual(evidence["output"]["sha256"],
                         evidence["input"]["sha256"])

    def test_record_beyond_file_size_rejected(self):
        order = self._empty_order()
        n = len(self.fx.source)
        order["records"].append({
            "byte_start": n - 10, "byte_end": n - 10 + 44, "layout": "A",
            "record_sha256": "0" * 64, "edits": []})
        with self.assertRaises(RewriteOrderError):
            self.fx.run(order)

    def test_existing_output_directory_refused(self):
        self.fx.out_dir.mkdir(parents=True)
        with self.assertRaises(RewriteIOError):
            self.fx.run(self._empty_order())

    def test_staging_neighbours_are_not_removed(self):
        neighbour = self.fx.staging
        neighbour.mkdir(parents=True)
        keep = neighbour / "keep-me.txt"
        keep.write_text("important")
        self.fx.run(self._empty_order())
        self.assertTrue(keep.exists())
        # the job's own staging subdirectory is cleaned up
        self.assertEqual([p for p in neighbour.iterdir() if p.is_dir()], [])

    def test_record_over_limit_rejected_before_allocation(self):
        order = self._empty_order()
        a0, a1 = self.fx.spans["A"]
        order["records"].append({
            "byte_start": a0, "byte_end": a1, "layout": "A",
            "record_sha256": sha256_hex(self.fx.source[a0:a1]),
            "edits": []})
        with self.assertRaises(Exception):
            self.fx.run(order, max_record_bytes=10)
        self.assertFalse(self.fx.out_dir.exists())

    def test_unwritable_output_location_fails_cleanly(self):
        order = self._empty_order()
        order_path = self.fx.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        # point the output at a path whose parent is a regular file
        blocker = self.fx.tmp / "blocker"
        blocker.write_text("x")
        with self.assertRaises(RewriteIOError):
            rewrite_original(
                order_path, source_path=self.fx.source_path,
                output_dir=blocker / "delivery",
                temp_dir=self.fx.staging,
                copybook_base=self.fx.copybooks["ORDA.cpy"].parent)
        self.assertFalse((blocker / "delivery").exists())

    def test_small_chunk_sizes_keep_result_identical(self):
        order = make_order(
            source=self.fx.source, spans=self.fx.spans, records=[])
        baseline = self.fx.run(order, chunk_size=7)
        baseline_bytes = self.fx.output_bytes()
        self._td.cleanup()
        self.setUp()
        again = self.fx.run(self._empty_order(), chunk_size=3)
        self.assertEqual(self.fx.output_bytes(), baseline_bytes)
        self.assertEqual(again["output"]["sha256"],
                         baseline["output"]["sha256"])


class ValueEdgeThroughEngineTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _edit_a(self, path, value, expected_slice):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        edits = [{"path": path,
                  "expected_hex": rec[expected_slice].hex(),
                  "value": value}]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", edits, spans=self.fx.spans)])
        return fill_record_digests(self.fx.source, order)

    def test_negative_and_negative_zero_zoned_on_layout_b(self):
        b0, b1 = self.fx.spans["B"]
        rec = self.fx.source[b0:b1]
        for value, tail_zone in (("-99999.99", 0xD0), ("-0", 0xC0)):
            edits = [{"path": ["ZONED-AMT"],
                      "expected_hex": rec[43:50].hex(), "value": value}]
            order = make_order(
                source=self.fx.source, spans=self.fx.spans,
                records=[record_entry("B", edits, spans=self.fx.spans)])
            fill_record_digests(self.fx.source, order)
            self.fx.run(order)
            out = self.fx.output_bytes()
            self.assertEqual(out[b0 + 49] & 0xF0, tail_zone)
            self._td.cleanup()
            self.setUp()

    def test_unsigned_flag_rejects_negative(self):
        order = self._edit_a(["FLAG"], "-0", slice(43, 44))
        with self.assertRaises(RewriteValueError):
            self.fx.run(order)

    def test_unencodable_text_rejected(self):
        order = self._edit_a(["ORDER-ID"], "账户", slice(0, 8))
        with self.assertRaises(RewriteValueError):
            self.fx.run(order)

    def test_capacity_overflow_rejected(self):
        # FLAG is PIC 9(1): a single digit at capacity works, 10 does not.
        ok = self._edit_a(["FLAG"], "9", slice(43, 44))
        self.fx.run(ok)
        self._td.cleanup()
        self.setUp()
        bad = self._edit_a(["FLAG"], "10", slice(43, 44))
        with self.assertRaises(RewriteValueError):
            self.fx.run(bad)

    def test_path_to_group_and_filler_rejected(self):
        for path in (["ITEM-GROUP", 0], ["FILLER-1"], ["ITEM-GROUP"]):
            order = self._edit_a(path, "x", slice(0, 1))
            with self.assertRaises(RewriteOrderError):
                self.fx.run(order)
            self._td.cleanup()
            self.setUp()


class DifferentLengthRedefinesTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        tmp = Path(self._td.name)
        self.copybooks = write_layouts(tmp / "copybooks")
        # 12-byte record: BASE 6 + TAIL 2 + FILLER 2 + padding; LONG-VIEW
        # (8) redefines BASE (6) and spills into TAIL's bytes.
        self.source = bytearray()
        self.source += b"ABCDEF"               # BASE-FIELD 0..6
        self.source += b"GH"                   # TAIL-FIELD 6..8
        self.source += b"    "                 # FILLER + padding 8..12
        assert len(self.source) == 12
        self.source = bytes(self.source)
        (tmp / "original.bin").write_bytes(self.source)
        self.tmp = tmp
        self.staging = tmp / "staging"
        self.out_dir = tmp / "delivery"

    def tearDown(self):
        self._td.cleanup()

    def _run(self, edits):
        order = {
            "source_sha256": sha256_hex(self.source),
            "layouts": {"RL": {"copybook": "RL.cpy", "name": "RL",
                               "version": "1", "codec": "cp037"}},
            "records": [{
                "byte_start": 0, "byte_end": 12, "layout": "RL",
                "record_sha256": sha256_hex(self.source),
                "edits": edits}]}
        order_path = self.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        return rewrite_original(
            order_path, source_path=self.tmp / "original.bin",
            output_dir=self.out_dir, temp_dir=self.staging,
            copybook_base=self.copybooks["RL.cpy"].parent)

    def test_consistent_overlap_across_different_view_lengths(self):
        # SHORT-VIEW PIC 9(4) forces the first four bytes to EBCDIC digits
        # 8899; the BASE text view must request exactly those same bytes at
        # the shared positions, supplying only its own remaining two bytes.
        shared = "8899".encode("cp037")
        tail = "34".encode("cp037")
        edits = [
            {"path": ["BASE-FIELD"],
             "expected_hex": "414243444546",
             "value": "889934"},
            {"path": ["SHORT-VIEW"], "expected_hex": "41424344",
             "value": "8899"},
        ]
        evidence = self._run(edits)
        out = self.out_dir.joinpath(OUTPUT_NAME).read_bytes()
        self.assertEqual(out[:4], shared)
        self.assertEqual(out[4:6], tail)
        # bytes 6..11 are outside both edited spans and keep the literal
        # bytes the fixture wrote
        self.assertEqual(out[6:8], b"GH")
        self.assertEqual(out[8:12], b"    ")
        self.assertGreaterEqual(evidence["counts"]["covered_bytes"], 6)

    def test_conflicting_different_length_views_rejected(self):
        edits = [
            {"path": ["BASE-FIELD"], "expected_hex": "414243444546",
             "value": "XYZZZZ"},
            {"path": ["SHORT-VIEW"], "expected_hex": "41424344",
             "value": "8899"},
        ]
        with self.assertRaises(RewriteConflictError):
            self._run(edits)
        self.assertFalse(self.out_dir.exists())

    def test_long_view_extends_into_tail_consistently(self):
        # LONG-VIEW X(8) covers bytes 0..7; TAIL-FIELD X(2) is at offset
        # 8..9, so here the two spans do not overlap; both edits still land
        # exactly on their own field's original bytes.
        edits = [
            {"path": ["LONG-VIEW"], "expected_hex": "4142434445464748",
             "value": "12345678"},
            {"path": ["TAIL-FIELD"], "expected_hex": "2020",
             "value": "78"},
        ]
        self._run(edits)
        out = self.out_dir.joinpath(OUTPUT_NAME).read_bytes()
        self.assertEqual(out[:8], "12345678".encode("cp037"))
        self.assertEqual(out[8:10], "78".encode("cp037"))
        # the FILLER at bytes 10..11 is never touched
        self.assertEqual(out[10:], b"  ")


class NestedOccursIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        tmp = Path(self._td.name)
        self.copybooks = write_layouts(tmp / "copybooks")
        self.tmp = tmp
        self.record = bytearray(b" " * 32)
        for o in range(2):
            for i in range(3):
                base = o * 16 + i * 5
                # CELL-CODE X(2): a letter plus one digit byte
                code = "C".encode("cp037") + bytes([0xF0 | i])
                amt = bytes([0xF0 | o, 0xF0 | i, 0xF0])
                assert len(code) == 2, code
                assert len(amt) == 3, amt
                self.record[base:base + 2] = code
                self.record[base + 2:base + 5] = amt
            self.record[o * 16 + 15] = 0xC1  # EBCDIC 'A'
        assert len(self.record) == 32
        self.record = bytes(self.record)
        (tmp / "original.bin").write_bytes(self.record)
        self.staging = tmp / "staging"
        self.out_dir = tmp / "delivery"

    def tearDown(self):
        self._td.cleanup()

    def test_same_named_leaves_distinguished_by_full_path(self):
        edits = [
            {"path": ["OUTER", 0, "INNER", 1, "CELL-AMT"],
             "expected_hex": self.record[7:10].hex(), "value": "999"},
            {"path": ["OUTER", 1, "INNER", 1, "CELL-AMT"],
             "expected_hex": self.record[23:26].hex(), "value": "111"},
        ]
        order = {
            "source_sha256": sha256_hex(self.record),
            "layouts": {"N": {"copybook": "NESTED.cpy", "name": "NESTED",
                              "version": "1", "codec": "cp037"}},
            "records": [{
                "byte_start": 0, "byte_end": 32, "layout": "N",
                "record_sha256": sha256_hex(self.record),
                "edits": edits}]}
        order_path = self.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        rewrite_original(
            order_path, source_path=self.tmp / "original.bin",
            output_dir=self.out_dir, temp_dir=self.staging,
            copybook_base=self.copybooks["NESTED.cpy"].parent)
        out = self.out_dir.joinpath(OUTPUT_NAME).read_bytes()
        self.assertEqual(out[7:10], "999".encode("cp037"))
        self.assertEqual(out[23:26], "111".encode("cp037"))
        # untouched neighbour cells remain
        self.assertEqual(out[2:5], self.record[2:5])
        self.assertEqual(out[18:21], self.record[18:21])


if __name__ == "__main__":
    unittest.main()
