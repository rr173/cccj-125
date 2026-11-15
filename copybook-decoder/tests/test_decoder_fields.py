"""Field decoding: byte widths, EBCDIC conversion, signs, implied decimals."""

import unittest
from decimal import Decimal

from copybook import (Decoder, FieldDecodeError, RecordLengthError,
                      parse_copybook)
from tests.builders import comp3, ebcdic, zoned

MIXED = """\
01  REC.
    05  NAME   PIC X(4).
    05  AMT    PIC S9(3)V99 COMP-3.
    05  CODE   PIC X(2).
"""


class TestByteWidths(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(MIXED)
        self.decoder = Decoder(self.layout)

    def test_record_length_counts_bytes_not_characters(self):
        # X(4) + S9(3)V99 COMP-3 (5 digits -> 3 bytes) + X(2)
        self.assertEqual(self.layout.record_length, 9)
        self.assertEqual(self.layout.find("AMT").byte_length, 3)
        self.assertEqual(self.layout.find("AMT").offset, 4)
        self.assertEqual(self.layout.find("CODE").offset, 7)

    def test_decode_mixed_record(self):
        data = ebcdic("ACME") + comp3(12345, 5) + ebcdic("OK")
        fields, errors = self.decoder.decode(data)
        self.assertEqual(errors, [])
        self.assertEqual(fields["NAME"], "ACME")
        self.assertEqual(fields["AMT"], Decimal("123.45"))
        self.assertEqual(fields["CODE"], "OK")

    def test_decoding_is_byte_based(self):
        # 0x81-0x84 are 'abcd' in cp037 but invalid as UTF-8: a decoder
        # working on decoded text instead of raw bytes would break here.
        raw_name = bytes([0x81, 0x82, 0x83, 0x84])
        with self.assertRaises(UnicodeDecodeError):
            raw_name.decode("utf-8")
        data = raw_name + comp3(100, 5) + ebcdic("XY")
        fields, _ = self.decoder.decode(data)
        self.assertEqual(fields["NAME"], "abcd")
        self.assertEqual(fields["AMT"], Decimal("1.00"))

    def test_length_mismatch_raises(self):
        with self.assertRaises(RecordLengthError) as ctx:
            self.decoder.decode(ebcdic("SHORT"))
        self.assertEqual(ctx.exception.expected, 9)
        self.assertEqual(ctx.exception.actual, 5)


class TestEncodingConversion(unittest.TestCase):
    def test_ebcdic_text_decodes(self):
        layout = parse_copybook("01 R. 05 A PIC X(5). 05 B PIC X(5).")
        fields, _ = Decoder(layout).decode(
            ebcdic("HELLO") + ebcdic("HI   "))
        self.assertEqual(fields["A"], "HELLO")
        self.assertEqual(fields["B"], "HI")  # trailing spaces stripped

    def test_codec_is_selectable(self):
        # 0x5A is '!' in cp037 but ']' in cp500
        raw = bytes([0x5A])
        f37, _ = Decoder(parse_copybook(
            "01 R. 05 A PIC X(1).", codec="cp037")).decode(raw)
        f500, _ = Decoder(parse_copybook(
            "01 R. 05 A PIC X(1).", codec="cp500")).decode(raw)
        self.assertEqual(f37["A"], "!")
        self.assertEqual(f500["A"], "]")


class TestZonedSignAndDecimals(unittest.TestCase):
    LAYOUT = """\
    01  R.
        05  POS   PIC S9(3).
        05  NEG   PIC S9(3).
        05  UNS   PIC 9(3).
    """

    def setUp(self):
        self.decoder = Decoder(parse_copybook(self.LAYOUT))

    def test_positive_overpunch(self):
        fields, _ = self.decoder.decode(
            zoned(123, 3) + zoned(0, 3) + zoned(7, 3, signed=False))
        self.assertEqual(fields["POS"], 123)
        self.assertEqual(fields["NEG"], 0)
        self.assertEqual(fields["UNS"], 7)

    def test_negative_overpunch(self):
        fields, _ = self.decoder.decode(
            zoned(-123, 3) + zoned(-1, 3) + zoned(999, 3, signed=False))
        self.assertEqual(fields["POS"], -123)
        self.assertEqual(fields["NEG"], -1)
        self.assertEqual(fields["UNS"], 999)

    def test_plain_digits_are_positive(self):
        fields, _ = self.decoder.decode(
            ebcdic("123") + ebcdic("456") + ebcdic("789"))
        self.assertEqual(fields["POS"], 123)
        self.assertEqual(fields["NEG"], 456)

    def test_implied_decimal_display(self):
        layout = parse_copybook(
            "01 R. 05 A PIC S9(5)V99. 05 B PIC S9(5)V99.")
        fields, _ = Decoder(layout).decode(zoned(12345, 7) + zoned(-999, 7))
        self.assertEqual(fields["A"], Decimal("123.45"))
        self.assertEqual(fields["B"], Decimal("-9.99"))

    def test_zoned_errors(self):
        with self.assertRaises(FieldDecodeError):
            self.decoder.decode(ebcdic("12 ") + zoned(1, 3) + zoned(1, 3))
        with self.assertRaises(FieldDecodeError):
            self.decoder.decode(ebcdic("1A3") + zoned(1, 3) + zoned(1, 3))


class TestComp3(unittest.TestCase):
    def test_sign_nibbles(self):
        layout = parse_copybook(
            "01 R. 05 A PIC S9(3) COMP-3. 05 B PIC S9(3) COMP-3."
            " 05 C PIC S9(3) COMP-3.")
        data = comp3(123, 3) + comp3(-123, 3) + comp3(123, 3, unsigned=True)
        fields, _ = Decoder(layout).decode(data)
        self.assertEqual(fields["A"], 123)
        self.assertEqual(fields["B"], -123)
        self.assertEqual(fields["C"], 123)  # F nibble = positive

    def test_implied_decimal(self):
        layout = parse_copybook("01 R. 05 A PIC S9(5)V99 COMP-3.")
        fields, _ = Decoder(layout).decode(comp3(12345, 7))
        self.assertEqual(fields["A"], Decimal("123.45"))
        fields, _ = Decoder(layout).decode(comp3(-1, 7))
        self.assertEqual(fields["A"], Decimal("-0.01"))

    def test_unsigned_picture_ignores_sign(self):
        layout = parse_copybook("01 R. 05 A PIC 9(3) COMP-3.")
        fields, _ = Decoder(layout).decode(comp3(-456, 3))
        self.assertEqual(fields["A"], 456)

    def test_invalid_packed_digit(self):
        layout = parse_copybook("01 R. 05 A PIC S9(3) COMP-3.")
        with self.assertRaises(FieldDecodeError) as ctx:
            Decoder(layout).decode(bytes([0x1A, 0x3C]))
        self.assertEqual(ctx.exception.field, "A")
        self.assertEqual(ctx.exception.offset, 0)

    def test_invalid_sign_nibble(self):
        layout = parse_copybook("01 R. 05 A PIC S9(3) COMP-3.")
        with self.assertRaises(FieldDecodeError):
            Decoder(layout).decode(bytes([0x12, 0x31]))


class TestGroupsAndOccurs(unittest.TestCase):
    def test_nested_groups_and_occurs_decode(self):
        layout = parse_copybook("""\
        01  REC.
            05  HDR PIC X(2).
            05  ITEM OCCURS 2 TIMES.
                10  QTY PIC 9(2).
                10  TAG PIC X(1).
        """)
        data = (ebcdic("H1")
                + ebcdic("01") + ebcdic("A")
                + ebcdic("02") + ebcdic("B"))
        fields, _ = Decoder(layout).decode(data)
        self.assertEqual(fields["HDR"], "H1")
        self.assertEqual(fields["ITEM"], [
            {"QTY": 1, "TAG": "A"},
            {"QTY": 2, "TAG": "B"},
        ])


if __name__ == "__main__":
    unittest.main()
