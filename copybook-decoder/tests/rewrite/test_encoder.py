"""Canonical value encoding: capacity, scale, sign and codec rules."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from copybook.parser import parse_copybook
from copybook.rewrite.encoder import encode_value
from copybook.rewrite.errors import RewriteValueError
from tests.rewrite.fixtures import (LAYOUT_A, LAYOUT_B, LAYOUT_WIDE,
                                    ref_comp3, ref_text, ref_zoned)


def field(layout, name):
    return layout.find(name)


class TextEncodingTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_A, codec="cp037")

    def test_right_pad_with_codec_space(self):
        got = encode_value("ACME", field(self.layout, "ORDER-ID"), "cp037")
        self.assertEqual(got, ref_text("ACME", 8, "cp037"))
        self.assertEqual(got, "ACME".encode("cp037") + b"\x40" * 4)

    def test_exact_capacity_no_padding(self):
        got = encode_value("12345678", field(self.layout, "ORDER-ID"),
                           "cp037")
        self.assertEqual(got, "12345678".encode("cp037"))

    def test_empty_string_becomes_all_spaces(self):
        got = encode_value("", field(self.layout, "ORDER-ID"), "cp037")
        self.assertEqual(got, b"\x40" * 8)

    def test_overflow_is_rejected_not_truncated(self):
        with self.assertRaises(RewriteValueError):
            encode_value("123456789", field(self.layout, "ORDER-ID"),
                         "cp037")

    def test_non_string_rejected(self):
        for bad in (123, 12.0, True, None, ["x"], {"x": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(RewriteValueError):
                    encode_value(bad, field(self.layout, "ORDER-ID"), "cp037")

    def test_unencodable_character_rejected_cp037(self):
        # CJK ideographs have no cp037/cp500 representation.
        with self.assertRaises(RewriteValueError):
            encode_value("账户", field(self.layout, "ORDER-ID"), "cp037")

    def test_cp500_text_independent_byte_check(self):
        layout_b = parse_copybook(LAYOUT_B, codec="cp500")
        got = encode_value("WORLD", field(layout_b, "REMARK"), "cp500")
        expected = ref_text("WORLD", 10, "cp500")
        self.assertEqual(got, expected)
        # Independent literal hex check, not via the production encoder.
        self.assertEqual(got.hex(), "e6d6d9d3c44040404040")
        self.assertEqual(got.decode("cp500"), "WORLD     ")


class NumericSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_WIDE)

    def test_only_decimal_strings_accepted(self):
        target = field(self.layout, "SCALED")
        for bad in (12, 12.5, -12.0, True, None, "1e3", "+1", " 1", "1 ",
                    "1,000", "1.", ".5", "0x10", "NaN", "Infinity", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(RewriteValueError):
                    encode_value(bad, target, "cp037")

    def test_fraction_scale_bounds(self):
        target = field(self.layout, "SCALED")  # S9(7)V99
        # one fraction digit is zero-padded on the right
        self.assertEqual(encode_value("12.3", target, "cp037"),
                         ref_comp3("12.30", 9, True, 2))
        # extra trailing zeroes still exceed the scale
        with self.assertRaises(RewriteValueError):
            encode_value("12.300", target, "cp037")
        with self.assertRaises(RewriteValueError):
            encode_value("12.345", target, "cp037")

    def test_leading_zeroes_and_plain_zero_allowed(self):
        target = field(self.layout, "SCALED")
        self.assertEqual(encode_value("00000000.00", target, "cp037"),
                         encode_value("0", target, "cp037"))
        self.assertEqual(encode_value("0", target, "cp037"),
                         bytes(4) + b"\x0c")  # 00 00 00 00 0C

    def test_unsigned_rejects_negative_including_negative_zero(self):
        unsigned = field(self.layout, "WIDE-DISP")
        with self.assertRaises(RewriteValueError):
            encode_value("-1", unsigned, "cp037")
        with self.assertRaises(RewriteValueError):
            encode_value("-0", unsigned, "cp037")

    def test_signed_negative_zero_written_as_non_negative(self):
        zoned = field(self.layout, "SCALED-ZONED")
        self.assertEqual(encode_value("-0", zoned, "cp037"),
                         encode_value("0", zoned, "cp037"))
        got = encode_value("-0", zoned, "cp037")
        self.assertEqual(got, ref_zoned("0.00", 7, True, scale=2))
        # the last byte carries the positive overpunch zone C, not D
        self.assertEqual(got[-1] & 0xF0, 0xC0)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_WIDE)

    def test_value_fitting_exactly_is_accepted(self):
        # 9(60) holds values strictly below 10**60; 10**60 - 1 fits.
        biggest = "9" * 60
        got = encode_value(biggest, field(self.layout, "WIDE-DISP"),
                           "cp037")
        self.assertEqual(len(got), 60)
        self.assertEqual(got, bytes(0xF9 for _ in range(60)))

    def test_one_beyond_capacity_rejected(self):
        overflow = "1" + "0" * 60  # == 10**60
        with self.assertRaises(RewriteValueError):
            encode_value(overflow, field(self.layout, "WIDE-DISP"), "cp037")

    def test_sixty_digit_precision_independent_check(self):
        value = "1234567890" * 6
        self.assertEqual(len(value), 60)
        signed_disp = bytearray(0xF0 | int(d) for d in value)
        signed_disp[-1] = 0xC0 | int(value[-1])
        cases = [
            ("WIDE-DISP", ref_zoned(value, 60, False)),
            ("WIDE-DISP-S", bytes(signed_disp)),
            ("WIDE-PACK", ref_comp3(value, 60, False)),
            ("WIDE-PACK-S", ref_comp3(value, 60, True)),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                got = encode_value(value, field(self.layout, name), "cp037")
                self.assertEqual(got, expected)
                self.assertEqual(len(got),
                                 60 if "DISP" in name else 31)

    def test_scaled_capacity_uses_scaled_integer(self):
        target = field(self.layout, "SCALED")  # 9 digits incl 2 fraction
        # scaled = 999999999 fits; 1000000000 does not
        encode_value("9999999.99", target, "cp037")
        with self.assertRaises(RewriteValueError):
            encode_value("10000000.00", target, "cp037")


class ZonedAndPackedBytesTests(unittest.TestCase):
    def setUp(self):
        self.layout_b = parse_copybook(LAYOUT_B, codec="cp500")
        self.layout_a = parse_copybook(LAYOUT_A, codec="cp037")

    def test_zoned_positive_and_negative_overpunch(self):
        target = self.layout_b.find("ZONED-AMT")  # S9(5)V99
        self.assertEqual(encode_value("-12.34", target, "cp500"),
                         ref_zoned("-12.34", 7, True, "cp500", 2))
        got = encode_value("-12.34", target, "cp500")
        self.assertEqual(got[-1] & 0xF0, 0xD0)          # negative zone D
        pos = encode_value("12.34", target, "cp500")
        self.assertEqual(pos[-1] & 0xF0, 0xC0)          # positive zone C

    def test_zoned_unsigned_uses_plain_f_zone(self):
        wide = parse_copybook(LAYOUT_WIDE)
        got = encode_value("42", wide.find("WIDE-DISP"), "cp037")
        self.assertEqual(got[-1] & 0xF0, 0xF0)

    def test_comp3_sign_and_padding_nibbles(self):
        target = self.layout_a.find("TOTAL")  # S9(7)V99, 5 bytes
        got = encode_value("9087.00", target, "cp037")
        self.assertEqual(got.hex(), "000908700c")
        neg = encode_value("-9087.00", target, "cp037")
        self.assertEqual(neg.hex(), "000908700d")
        unsigned = parse_copybook(LAYOUT_WIDE).find("WIDE-PACK")
        self.assertEqual(encode_value("0", unsigned, "cp037")[-1] & 0x0F,
                         0xF)

    def test_deterministic_canonical_bytes(self):
        target = self.layout_a.find("TOTAL")
        first = encode_value("12345.67", target, "cp037")
        for _ in range(5):
            self.assertEqual(
                encode_value("12345.67", target, "cp037"), first)


if __name__ == "__main__":
    unittest.main()
