"""Correction-order loading: strict JSON, schema and coordinate rules."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from copybook.rewrite.errors import RewriteOrderError
from copybook.rewrite.order import load_order
from tests.rewrite.fixtures import write_layouts


def write_order(directory: Path, payload) -> Path:
    p = directory / "order.json"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def base_order(tmp: Path, records=None, *, source_hex="a" * 64):
    write_layouts(tmp)
    return {
        "source_sha256": source_hex,
        "layouts": {
            "A": {"copybook": "ORDA.cpy", "name": "ORD-A", "version": "1",
                  "codec": "cp037"},
            "B": {"copybook": "SUMB.cpy", "name": "SUM-B", "version": "1",
                  "codec": "cp500"},
        },
        "records": records if records is not None else [],
    }


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_object_member_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = (
                '{"source_sha256": "' + "a" * 64 + '", '
                '"layouts": {"A": {"copybook": "ORDA.cpy", "name": "N", '
                '"version": "1", "codec": "cp037"}}, '
                '"records": [], "records": []}')
            write_layouts(tmp)
            p = write_order(tmp, payload)
            with self.assertRaises(RewriteOrderError):
                load_order(p)

    def test_nan_and_infinity_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for token in ("NaN", "Infinity", "-Infinity"):
                payload = base_order(tmp, source_hex="b" * 64)
                payload["records"] = [{
                    "byte_start": 0, "byte_end": 44, "layout": "A",
                    "record_sha256": "c" * 64,
                    "edits": [{"path": ["FLAG"], "expected_hex": "f7",
                               "value": token}]}]
                # inject the bare token as raw JSON
                text = json.dumps(payload).replace(
                    json.dumps(token), token)
                p = write_order(tmp, text)
                with self.assertRaises(RewriteOrderError):
                    load_order(p)

    def test_float_rejected_even_when_integral_valued(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp)
            text = json.dumps(payload).replace('"records": []',
                                                '"records": [], "x": 1.0')
            p = write_order(tmp, text)
            with self.assertRaises(RewriteOrderError):
                load_order(p)

    def test_big_integer_accepted(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": 10 ** 40,
                "byte_end": 10 ** 40 + 44, "layout": "A",
                "record_sha256": "d" * 64, "edits": []}])
            order = load_order(write_order(tmp, payload))
            self.assertEqual(order.records[0].byte_start, 10 ** 40)


class SchemaTests(unittest.TestCase):
    def test_missing_top_level_members(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            write_layouts(tmp)
            for member in ("source_sha256", "layouts", "records"):
                payload = base_order(tmp)
                del payload[member]
                with self.assertRaises(RewriteOrderError):
                    load_order(write_order(tmp, payload))

    def test_unknown_codec_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp)
            payload["layouts"]["A"]["codec"] = "utf-8"
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_unknown_layout_reference(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": 0, "byte_end": 44, "layout": "ZZZ",
                "record_sha256": "e" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_missing_copybook_file(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp)
            payload["layouts"]["A"]["copybook"] = "missing.cpy"
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_missing_edit_members(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": 0, "byte_end": 44, "layout": "A",
                "record_sha256": "f" * 64,
                "edits": [{"path": ["FLAG"], "expected_hex": "f7"}]}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))


class CoordinateTests(unittest.TestCase):
    def _record(self, start, end, layout="A", edits=None):
        length = 44 if layout == "A" else 50
        self.assertEqual(end - start, length)
        return {"byte_start": start, "byte_end": end, "layout": layout,
                "record_sha256": "0" * 64,
                "edits": edits if edits is not None else []}

    def test_bool_is_not_an_integer_coordinate(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": True, "byte_end": 44, "layout": "A",
                "record_sha256": "1" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_negative_coordinates_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": -1, "byte_end": 43, "layout": "A",
                "record_sha256": "2" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_half_open_interval_must_have_positive_length(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": 10, "byte_end": 10, "layout": "A",
                "record_sha256": "3" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_overlapping_and_duplicate_spans_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for records in (
                    [self._record(0, 44), self._record(40, 84)],
                    [self._record(0, 44), self._record(0, 44)],
                    [self._record(100, 144), self._record(50, 94),
                     self._record(90, 134)]):
                payload = base_order(tmp, records=records)
                with self.assertRaises(RewriteOrderError):
                    load_order(write_order(tmp, payload))

    def test_touching_spans_and_gaps_are_legal_and_sorted(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # shuffled input, non-overlapping, with a gap and adjacency
            records = [self._record(100, 144), self._record(0, 44),
                       self._record(44, 88)]
            payload = base_order(tmp, records=records)
            order = load_order(write_order(tmp, payload))
            self.assertEqual([r.byte_start for r in order.records],
                             [0, 44, 100])

    def test_record_length_must_match_layout(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[{
                "byte_start": 0, "byte_end": 45, "layout": "A",
                "record_sha256": "4" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))
            # a span whose length matches the *other* layout is still wrong
            payload = base_order(tmp, records=[{
                "byte_start": 0, "byte_end": 50, "layout": "A",
                "record_sha256": "4" * 64, "edits": []}])
            with self.assertRaises(RewriteOrderError):
                load_order(write_order(tmp, payload))

    def test_empty_records_and_empty_edits_are_legal(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = base_order(tmp, records=[])
            order = load_order(write_order(tmp, payload))
            self.assertEqual(order.records, [])
            payload = base_order(tmp, records=[self._record(0, 44, edits=[])])
            order = load_order(write_order(tmp, payload))
            self.assertEqual(order.records[0].edits, [])


class EditPathShapeTests(unittest.TestCase):
    def test_bool_and_float_path_components_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for bad_path in (["FLAG", True], ["FLAG", 1.5], [0],
                             ["FLAG", -1]):
                payload = base_order(tmp, records=[{
                    "byte_start": 0, "byte_end": 44, "layout": "A",
                    "record_sha256": "5" * 64,
                    "edits": [{"path": bad_path, "expected_hex": "00",
                               "value": "x"}]}])
                with self.assertRaises(RewriteOrderError):
                    load_order(write_order(tmp, payload))

    def test_bad_expected_hex_shape_rejected(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for bad_hex in ("", "zz", "abc", "0"):
                payload = base_order(tmp, records=[{
                    "byte_start": 0, "byte_end": 44, "layout": "A",
                    "record_sha256": "6" * 64,
                    "edits": [{"path": ["FLAG"], "expected_hex": bad_hex,
                               "value": "9"}]}])
                with self.assertRaises(RewriteOrderError):
                    load_order(write_order(tmp, payload))


if __name__ == "__main__":
    unittest.main()
