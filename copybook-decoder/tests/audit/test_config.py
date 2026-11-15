"""Unit tests for audit configuration, field paths and exact quantities."""

import json
import tempfile
import unittest
from pathlib import Path

from copybook.audit import (AuditConfig, AuditConfigError, FieldPath,
                            format_milli, parse_count, parse_quantity_milli)


def _cfg_dict():
    return {
        "categories": ["A", "B", "C"],
        "paths": {
            "record_type": ["REC-TYPE"],
            "file_id": ["FILE-ID"],
            "batch_id": ["BATCH-ID"],
            "seq": ["SEQ"],
            "category": ["CATEGORY"],
            "quantity": ["QTY"],
            "btail_detail_count": ["DETAIL-COUNT"],
            "btail_totals": {
                "A": ["TOTAL-A"],
                "B": ["TOTAL-B"],
                "C": ["TOTAL-C"],
            },
            "ftail_batch_count": ["BATCH-COUNT"],
            "ftail_detail_count": ["DETAIL-COUNT"],
        },
    }


class TestQuantity(unittest.TestCase):
    def test_exact_milli_conversion(self):
        cases = {
            "0": 0,
            "1": 1000,
            "1.0": 1000,
            "1.000": 1000,
            "1.234": 1234,
            "0.001": 1,
            "-0.001": -1,
            "-123.456": -123456,
            "+7.5": 7500,
            "999999999.999": 999999999999,
            "-0.000": 0,
        }
        for text, milli in cases.items():
            self.assertEqual(parse_quantity_milli(text), milli, text)

    def test_trailing_digits_are_preserved(self):
        # no float: 0.1 + 0.2 style cases must stay exact in milli units
        self.assertEqual(parse_quantity_milli("0.300"),
                         parse_quantity_milli("0.100")
                         + parse_quantity_milli("0.200"))

    def test_illegal_quantity_strings(self):
        for bad in ("1.2345", "1.", ".5", "", " 1", "1 ", "1e3", "NaN",
                    "1,000", "--1", "++2", "abc", "0x10", None):
            self.assertIsNone(parse_quantity_milli(bad), repr(bad))

    def test_format_roundtrip(self):
        for milli in (0, 1, -1, 1234, -123456, 1000000, -999):
            self.assertEqual(parse_quantity_milli(format_milli(milli)),
                             milli)

    def test_parse_count(self):
        self.assertEqual(parse_count("0"), 0)
        self.assertEqual(parse_count("123"), 123)
        for bad in ("-1", "1.0", "", None, "x", "1 "):
            self.assertIsNone(parse_count(bad), repr(bad))


class TestFieldPath(unittest.TestCase):
    def test_dict_and_list_navigation(self):
        fields = {"H": {"B": [{"Q": "5"}, {"Q": "7"}]}, "X": None}
        self.assertEqual(FieldPath.parse(["H", "B", "1", "Q"]).get(fields),
                         "7")
        self.assertEqual(FieldPath.parse(["H", "B", "9", "Q"]).get(fields),
                         None)
        self.assertIsNone(FieldPath.parse(["H", "B", "x"]).get(fields))
        self.assertIsNone(FieldPath.parse(["NOPE"]).get(fields))
        self.assertIsNone(FieldPath.parse(["X", "Y"]).get(fields))
        # list-valued leaf is returned as-is, not coerced
        self.assertEqual(FieldPath.parse(["H", "B"]).get(fields),
                         [{"Q": "5"}, {"Q": "7"}])

    def test_invalid_path_specs(self):
        for bad in ([], "x", ["", "A"], [1], [None]):
            with self.assertRaises(AuditConfigError):
                FieldPath.parse(bad)


class TestAuditConfig(unittest.TestCase):
    def test_valid_config(self):
        cfg = AuditConfig.from_dict(_cfg_dict())
        self.assertEqual(cfg.categories, ("A", "B", "C"))
        self.assertEqual(cfg.btail_total_paths["B"].parts, ("TOTAL-B",))

    def test_duplicate_category_rejected(self):
        data = _cfg_dict()
        data["categories"] = ["A", "A"]
        with self.assertRaises(AuditConfigError):
            AuditConfig.from_dict(data)

    def test_totals_must_cover_exactly_categories(self):
        data = _cfg_dict()
        del data["paths"]["btail_totals"]["C"]
        with self.assertRaises(AuditConfigError):
            AuditConfig.from_dict(data)
        data = _cfg_dict()
        data["paths"]["btail_totals"]["D"] = ["TOTAL-D"]
        with self.assertRaises(AuditConfigError):
            AuditConfig.from_dict(data)

    def test_missing_path_key(self):
        data = _cfg_dict()
        del data["paths"]["seq"]
        with self.assertRaises(AuditConfigError):
            AuditConfig.from_dict(data)

    def test_load_from_file_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(AuditConfigError):
                AuditConfig.load(p)
            p.write_text(json.dumps(_cfg_dict()), encoding="utf-8")
            cfg = AuditConfig.load(p)
            self.assertEqual(cfg.categories[0], "A")


if __name__ == "__main__":
    unittest.main()
