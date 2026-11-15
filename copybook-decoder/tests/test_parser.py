"""Parser tests: layout tree, byte offsets, OCCURS, REDEFINES, errors."""

import unittest

from copybook import CopybookSyntaxError, parse_copybook

BASIC = """\
      * ACCT daily feed layout
       01  ACCT-RECORD.
           05  ACCT-ID          PIC X(10).
           05  NAME-GROUP.
               10  FIRST-NAME   PIC X(8).
               10  LAST-NAME    PIC X(12).
           05  BALANCE          PIC S9(7)V99 COMP-3.
           05  TXN-DATE         PIC 9(8).
"""


class TestOffsets(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(BASIC, name="ACCT", version="1")

    def test_record_length_is_bytes(self):
        # 10 + (8+12) + 5 packed bytes + 8
        self.assertEqual(self.layout.record_length, 43)

    def test_group_and_leaf_offsets(self):
        expected = {
            "ACCT-ID": (0, 10),
            "NAME-GROUP": (10, 20),
            "FIRST-NAME": (10, 8),
            "LAST-NAME": (18, 12),
            "BALANCE": (30, 5),   # S9(7)V99 COMP-3: 9 digits -> 5 bytes
            "TXN-DATE": (35, 8),
        }
        for name, (offset, length) in expected.items():
            with self.subTest(field=name):
                f = self.layout.find(name)
                self.assertIsNotNone(f)
                self.assertEqual(f.offset, offset)
                self.assertEqual(f.byte_length, length)

    def test_comp3_byte_width_is_not_digit_count(self):
        text = "01 R. 05 A PIC S9(4) COMP-3. 05 B PIC S9(7)V99 COMP-3."
        layout = parse_copybook(text)
        self.assertEqual(layout.find("A").byte_length, 3)  # 4 digits -> 3
        self.assertEqual(layout.find("B").byte_length, 5)  # 9 digits -> 5
        self.assertEqual(layout.record_length, 8)

    def test_pic_variants(self):
        text = """\
       01  R.
           05  A PIC XXX.
           05  B PIC 9.
           05  C PIC A(2).
           05  D PIC S9(3)V9(2).
           05  E PIC 99V99.
       """
        layout = parse_copybook(text)
        self.assertEqual(layout.record_length, 3 + 1 + 2 + 5 + 4)
        d = layout.find("D")
        self.assertTrue(d.pic.signed)
        self.assertEqual(d.pic.scale, 2)
        e = layout.find("E")
        self.assertFalse(e.pic.signed)
        self.assertEqual(e.pic.scale, 2)


class TestOccurs(unittest.TestCase):
    def test_occurs_group_offsets(self):
        text = """\
       01  REC.
           05  HDR     PIC X(2).
           05  ITEM    OCCURS 3 TIMES.
               10  QTY   PIC 9(2).
               10  TAG   PIC X(1).
           05  FOOTER  PIC X(1).
       """
        layout = parse_copybook(text)
        item = layout.find("ITEM")
        self.assertEqual(item.offset, 2)
        self.assertEqual(item.byte_length, 3)
        self.assertEqual(item.total_length, 9)
        self.assertEqual(layout.find("FOOTER").offset, 11)
        self.assertEqual(layout.record_length, 12)

    def test_occurs_elementary(self):
        text = "01 R. 05 CODES PIC X(2) OCCURS 4 TIMES. 05 TAIL PIC X(1)."
        layout = parse_copybook(text)
        self.assertEqual(layout.find("CODES").total_length, 8)
        self.assertEqual(layout.find("TAIL").offset, 8)


class TestRedefinesOffsets(unittest.TestCase):
    def test_redefines_shares_target_offset(self):
        text = """\
       01  REC.
           05  A  PIC X(2).
           05  B  REDEFINES A PIC 9(2).
           05  C  PIC X(1).
       """
        layout = parse_copybook(text)
        self.assertEqual(layout.find("B").offset, 0)
        self.assertEqual(layout.find("C").offset, 2)
        self.assertEqual(layout.record_length, 3)

    def test_longer_view_extends_following_offset(self):
        text = """\
       01  REC.
           05  A  PIC X(2).
           05  B  REDEFINES A PIC X(4).
           05  C  PIC X(1).
       """
        layout = parse_copybook(text)
        self.assertEqual(layout.find("C").offset, 4)
        self.assertEqual(layout.record_length, 5)


class TestRobustParsing(unittest.TestCase):
    def test_comments_and_multiline_statements(self):
        text = """\
      * full line comment
       01  REC.                                        *> inline comment
           05  A
               PIC X(2)
               OCCURS 2 TIMES.
           05  B PIC 9(2). *> trailing comment
       """
        layout = parse_copybook(text)
        self.assertEqual(layout.record_length, 4 + 2)

    def test_filler_gets_unique_names(self):
        text = """\
       01  REC.
           05  A PIC X(1).
           05  FILLER PIC X(2).
           05  FILLER PIC X(3).
       """
        layout = parse_copybook(text)
        self.assertEqual(layout.record_length, 6)
        names = [f.name for f in layout.root.children]
        self.assertEqual(names, ["A", "FILLER-1", "FILLER-2"])


class TestSyntaxErrors(unittest.TestCase):
    def assert_bad(self, text):
        with self.assertRaises(CopybookSyntaxError):
            parse_copybook(text)

    def test_unsupported_levels(self):
        self.assert_bad("01 R. 66 X RENAMES A.")
        self.assert_bad("77 X PIC X.")
        self.assert_bad("01 R. 05 A PIC X. 88 COND VALUE 'Y'.")

    def test_unsupported_clauses(self):
        self.assert_bad("01 R. 05 A PIC X VALUE 'Y'.")
        self.assert_bad("01 R. 05 A PIC 9(4) COMP.")
        self.assert_bad("01 R. 05 A PIC 9(4) BINARY.")
        self.assert_bad("01 R. 05 A PIC X(2) JUSTIFIED RIGHT.")
        self.assert_bad("01 R. 05 A PIC S9(3) SIGN LEADING.")
        self.assert_bad("01 R. 05 A PIC S9(3) SIGN TRAILING SEPARATE.")

    def test_occurs_depending_on_rejected(self):
        self.assert_bad("01 R. 05 N PIC 9. 05 A PIC X(1) OCCURS 1 TO 5 "
                        "DEPENDING ON N.")

    def test_bad_pictures(self):
        self.assert_bad("01 R. 05 A PIC 9X(3).")       # mixed categories
        self.assert_bad("01 R. 05 A PIC ZZ9.")         # edited picture
        self.assert_bad("01 R. 05 A PIC X(0).")        # zero repeat
        self.assert_bad("01 R. 05 A PIC X(2.")         # unbalanced paren
        self.assert_bad("01 R. 05 A PIC SX(3).")       # signed alpha
        self.assert_bad("01 R. 05 A PIC X(2)V9.")      # V in alpha

    def test_structure_errors(self):
        self.assert_bad("")                                       # empty
        self.assert_bad("05 A PIC X(2).")                         # no 01
        self.assert_bad("01 A PIC X. 01 B PIC X.")                # two 01
        self.assert_bad("01 R. 05 G. ")                           # empty group
        self.assert_bad("01 R. 05 A PIC X(2). 10 B PIC X(1).")    # sub of elem
        self.assert_bad("01 R. 05 A REDEFINES NOPE PIC X(2).")    # no target
        self.assert_bad("01 R REDEFINES R. 05 A PIC X.")          # 01 redefines
        self.assert_bad("01 R. 05 A PIC X(2) UNKNOWN-CLAUSE.")
        self.assert_bad("01 R. 05 A PIC X(2) COMP-3.")            # alpha comp3


if __name__ == "__main__":
    unittest.main()
