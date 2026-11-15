"""REDEFINES: alternative views, selection policies, error tolerance."""

import unittest
from decimal import Decimal

from copybook import (Decoder, FieldDecodeError, RedefinesPolicy,
                      parse_copybook)
from tests.builders import ebcdic, zoned

FLEX_LAYOUT = """\
01  REC.
    05  REC-TYPE    PIC X(1).
    05  FLEX        PIC X(8).
    05  FLEX-AMT    REDEFINES FLEX PIC S9(6)V99.
    05  FLEX-DATE   REDEFINES FLEX PIC 9(8).
    05  TAIL        PIC X(1).
"""


def flex_record(rec_type: bytes, flex: bytes, tail: bytes = b"\xd9") -> bytes:
    return rec_type + flex + tail  # 1 + 8 + 1 = 10 bytes


class TestAllViews(unittest.TestCase):
    def test_all_views_decoded_by_default(self):
        layout = parse_copybook(FLEX_LAYOUT)
        data = flex_record(ebcdic("A"), zoned(123456, 8))
        fields, errors = Decoder(layout).decode(data)
        self.assertEqual(errors, [])
        self.assertEqual(fields["REC-TYPE"], "A")
        self.assertEqual(fields["FLEX"], "0012345F")   # raw zoned bytes
        self.assertEqual(fields["FLEX-AMT"], Decimal("1234.56"))
        self.assertEqual(fields["FLEX-DATE"], 123456)
        self.assertEqual(fields["TAIL"], "R")

    def test_views_share_the_same_bytes(self):
        layout = parse_copybook(FLEX_LAYOUT)
        for name in ("FLEX", "FLEX-AMT", "FLEX-DATE"):
            self.assertEqual(layout.find(name).offset, 1)


class TestViewSelection(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(FLEX_LAYOUT)
        self.data = flex_record(ebcdic("A"), zoned(123456, 8))

    def test_include_list(self):
        policy = RedefinesPolicy(include=("FLEX-DATE",))
        fields, _ = Decoder(self.layout, policy).decode(self.data)
        self.assertIn("FLEX-DATE", fields)
        self.assertNotIn("FLEX-AMT", fields)
        self.assertIn("FLEX", fields)  # base field always decoded

    def test_discriminator_selects_view(self):
        policy = RedefinesPolicy(
            discriminator="REC-TYPE",
            cases={"A": "FLEX-AMT", "D": "FLEX-DATE"})
        fields, _ = Decoder(self.layout, policy).decode(self.data)
        self.assertIn("FLEX-AMT", fields)
        self.assertNotIn("FLEX-DATE", fields)

    def test_discriminator_other_case(self):
        policy = RedefinesPolicy(
            discriminator="REC-TYPE",
            cases={"A": "FLEX-AMT", "D": "FLEX-DATE"})
        data = flex_record(ebcdic("D"), zoned(20240131, 8))
        fields, _ = Decoder(self.layout, policy).decode(data)
        self.assertEqual(fields["FLEX-DATE"], 20240131)
        self.assertNotIn("FLEX-AMT", fields)

    def test_discriminator_no_match_no_default(self):
        policy = RedefinesPolicy(
            discriminator="REC-TYPE", cases={"A": "FLEX-AMT"})
        data = flex_record(ebcdic("Z"), zoned(1, 8))
        fields, _ = Decoder(self.layout, policy).decode(data)
        self.assertNotIn("FLEX-AMT", fields)
        self.assertNotIn("FLEX-DATE", fields)
        self.assertIn("FLEX", fields)

    def test_discriminator_default_view(self):
        policy = RedefinesPolicy(
            discriminator="REC-TYPE", cases={"A": "FLEX-AMT"},
            default="FLEX-DATE")
        data = flex_record(ebcdic("Z"), zoned(20240131, 8))
        fields, _ = Decoder(self.layout, policy).decode(data)
        self.assertEqual(fields["FLEX-DATE"], 20240131)
        self.assertNotIn("FLEX-AMT", fields)

    def test_include_and_discriminator_are_exclusive(self):
        with self.assertRaises(ValueError):
            RedefinesPolicy(include=("FLEX-AMT",), discriminator="REC-TYPE")


class TestViewErrorTolerance(unittest.TestCase):
    def test_invalid_view_is_not_fatal(self):
        layout = parse_copybook(FLEX_LAYOUT)
        # letters in FLEX break both numeric views but not the base field
        data = flex_record(ebcdic("A"), ebcdic("ABCDEFGH"))
        fields, errors = Decoder(layout).decode(data)
        self.assertEqual(fields["FLEX"], "ABCDEFGH")
        self.assertIsNone(fields["FLEX-AMT"])
        self.assertIsNone(fields["FLEX-DATE"])
        self.assertEqual(len(errors), 2)
        self.assertEqual({e.field for e in errors},
                         {"FLEX-AMT", "FLEX-DATE"})
        # error offsets point at the view's bytes within the record
        self.assertTrue(all(e.offset == 1 for e in errors))

    def test_base_field_error_is_fatal(self):
        layout = parse_copybook(
            "01 R. 05 N PIC 9(2). 05 V REDEFINES N PIC X(2).")
        with self.assertRaises(FieldDecodeError):
            Decoder(layout).decode(ebcdic("AB"))


if __name__ == "__main__":
    unittest.main()
