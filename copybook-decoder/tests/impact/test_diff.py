"""compare_layouts: change categories and compatibility conclusions."""

import unittest

from copybook import parse_copybook
from copybook.impact import (ADDED, COMPATIBLE, INCOMPATIBLE, MOVED,
                             REINTERPRETED, REMOVED, UNCHANGED,
                             UNVERIFIABLE, compare_layouts, flatten_layout)


def flat(text, version="1", codec="cp037"):
    return flatten_layout(parse_copybook(text, name="T", version=version,
                                         codec=codec))


def compare(old_text, new_text, old_codec="cp037", new_codec="cp037"):
    return compare_layouts(flat(old_text, "1", old_codec),
                           flat(new_text, "2", new_codec),
                           "old", "new")


def categories(cmp):
    return {f.path_text: f.category for f in cmp.fields}


class TestCategories(unittest.TestCase):
    def test_unchanged(self):
        cmp = compare("01  R.\n  05  A  PIC X(3).\n",
                      "01  R.\n  05  A  PIC X(3).\n")
        self.assertEqual(categories(cmp), {"R/A": UNCHANGED})
        self.assertEqual(cmp.conclusion, COMPATIBLE)

    def test_moved_only_by_preceding_resize(self):
        old = "01  R.\n  05  A  PIC X(3).\n  05  B  PIC 9(4).\n"
        new = "01  R.\n  05  A  PIC X(5).\n  05  B  PIC 9(4).\n"
        cmp = compare(old, new)
        cats = categories(cmp)
        self.assertEqual(cats["R/A"], REINTERPRETED)   # own PIC changed
        self.assertEqual(cats["R/B"], MOVED)           # only shifted
        moved = next(f for f in cmp.fields if f.path_text == "R/B")
        self.assertEqual((moved.old.offset, moved.new.offset), (3, 5))
        self.assertIn("field definition unchanged", moved.note)
        self.assertEqual(cmp.conclusion, INCOMPATIBLE)

    def test_scale_change_at_constant_width_is_not_a_move(self):
        old = "01  R.\n  05  B  PIC S9(7)V99 COMP-3.\n  05  T  PIC X(1).\n"
        new = "01  R.\n  05  B  PIC S9(6)V999 COMP-3.\n  05  T  PIC X(1).\n"
        cmp = compare(old, new)
        cats = categories(cmp)
        self.assertEqual(cats["R/B"], REINTERPRETED)
        self.assertEqual(cats["R/T"], UNCHANGED)   # 5 bytes both sides
        change = next(f for f in cmp.fields if f.path_text == "R/B")
        self.assertIn("scale: 2 -> 3", change.changes)
        self.assertFalse(any("offset" in c for c in change.changes))

    def test_sign_change(self):
        cmp = compare("01  R.\n  05  N  PIC 9(4).\n",
                      "01  R.\n  05  N  PIC S9(4).\n")
        change = next(f for f in cmp.fields if f.path_text == "R/N")
        self.assertEqual(change.category, REINTERPRETED)
        self.assertIn("signed: False -> True", change.changes)

    def test_occurs_change(self):
        cmp = compare("01  R.\n  05  N  PIC 9(2) OCCURS 3 TIMES.\n",
                      "01  R.\n  05  N  PIC 9(2) OCCURS 5 TIMES.\n")
        change = next(f for f in cmp.fields if f.path_text == "R/N")
        self.assertEqual(change.category, REINTERPRETED)
        self.assertIn("occurs: 3 -> 5", change.changes)

    def test_codec_change_flags_display_but_not_comp3(self):
        old = ("01  R.\n  05  T  PIC X(2).\n"
               "  05  P  PIC S9(3) COMP-3.\n")
        cmp = compare(old, old, new_codec="cp500")
        cats = categories(cmp)
        self.assertEqual(cats["R/T"], REINTERPRETED)
        self.assertEqual(cats["R/P"], UNCHANGED)   # packed: codec-free
        self.assertEqual(cmp.conclusion, INCOMPATIBLE)

    def test_added_and_removed(self):
        old = "01  R.\n  05  A  PIC X(2).\n  05  B  PIC X(2).\n"
        new = "01  R.\n  05  A  PIC X(2).\n  05  C  PIC X(2).\n"
        cmp = compare(old, new)
        cats = categories(cmp)
        self.assertEqual(cats["R/B"], REMOVED)
        self.assertEqual(cats["R/C"], ADDED)
        self.assertEqual(cmp.conclusion, INCOMPATIBLE)


class TestCompatibilityRules(unittest.TestCase):
    def test_added_view_is_compatible(self):
        old = "01  R.\n  05  A  PIC X(4).\n"
        new = ("01  R.\n  05  A  PIC X(4).\n"
               "  05  A-NUM  REDEFINES A PIC 9(4).\n")
        cmp = compare(old, new)
        self.assertEqual(categories(cmp)["R/A-NUM"], ADDED)
        self.assertEqual(cmp.conclusion, COMPATIBLE)
        added = next(f for f in cmp.fields if f.path_text == "R/A-NUM")
        self.assertIn("alternative reading", added.note)
        self.assertEqual(len(cmp.view_changes), 1)
        vc = cmp.view_changes[0]
        self.assertEqual(vc.old_views, ())
        self.assertEqual(vc.new_views, (("R", "A-NUM"),))

    def test_added_field_in_former_filler_is_compatible(self):
        old = "01  R.\n  05  A  PIC X(2).\n  05  FILLER  PIC X(3).\n"
        new = "01  R.\n  05  A  PIC X(2).\n  05  B  PIC X(3).\n"
        cmp = compare(old, new)
        self.assertEqual(categories(cmp)["R/B"], ADDED)
        self.assertEqual(cmp.record_length_old, cmp.record_length_new)
        self.assertEqual(cmp.conclusion, COMPATIBLE)
        added = next(f for f in cmp.fields if f.path_text == "R/B")
        self.assertIn("FILLER", added.note)

    def test_added_field_extending_record_is_incompatible(self):
        old = "01  R.\n  05  A  PIC X(2).\n"
        new = "01  R.\n  05  A  PIC X(2).\n  05  B  PIC X(3).\n"
        cmp = compare(old, new)
        self.assertEqual(cmp.conclusion, INCOMPATIBLE)
        self.assertEqual(cmp.record_length_new - cmp.record_length_old, 3)

    def test_removed_into_filler_still_incompatible(self):
        old = "01  R.\n  05  A  PIC X(2).\n  05  B  PIC X(3).\n"
        new = "01  R.\n  05  A  PIC X(2).\n  05  FILLER  PIC X(3).\n"
        cmp = compare(old, new)
        removed = next(f for f in cmp.fields if f.path_text == "R/B")
        self.assertEqual(removed.category, REMOVED)
        self.assertIn("FILLER", removed.note)
        self.assertEqual(cmp.conclusion, INCOMPATIBLE)


class TestRecordLengthExplanation(unittest.TestCase):
    def test_contributors_add_up(self):
        old = ("01  R.\n  05  KEEP  PIC X(2).\n  05  GONE  PIC X(2).\n"
               "  05  FILLER  PIC X(1).\n")
        new = ("01  R.\n  05  KEEP  PIC X(2).\n  05  NEWFIELD  PIC X(4).\n"
               "  05  FILLER  PIC X(2).\n")
        cmp = compare(old, new)
        delta = cmp.record_length_new - cmp.record_length_old
        self.assertEqual(delta, 3)                    # 5 -> 8
        self.assertEqual(sum(i["bytes"] for i in cmp.length_explanation),
                         delta)
        effects = {(i["path"], i["effect"]): i["bytes"]
                   for i in cmp.length_explanation}
        self.assertEqual(effects[("R/GONE", "removed")], -2)
        self.assertEqual(effects[("R/NEWFIELD", "added")], 4)
        self.assertEqual(effects[("(FILLER)", "filler resized")], 1)


class TestNamesAndPaths(unittest.TestCase):
    def test_same_name_different_group_is_remove_plus_add(self):
        old = ("01  R.\n  05  GRP-OLD.\n    10  S  PIC X(2).\n")
        new = ("01  R.\n  05  GRP-NEW.\n    10  S  PIC X(2).\n")
        cmp = compare(old, new)
        cats = categories(cmp)
        self.assertEqual(cats["R/GRP-OLD/S"], REMOVED)
        self.assertEqual(cats["R/GRP-NEW/S"], ADDED)
        self.assertTrue(any("different group paths" in n
                            for n in cmp.notes))

    def test_shared_name_note(self):
        text = ("01  R.\n  05  G1.\n    10  C  PIC X(1).\n"
                "  05  G2.\n    10  C  PIC X(1).\n")
        cmp = compare(text, text)
        self.assertTrue(any("multiple groups" in n for n in cmp.notes))
        self.assertEqual(cmp.conclusion, COMPATIBLE)

    def test_duplicate_path_is_unverifiable(self):
        old = "01  R.\n  05  A  PIC X(2).\n"
        new = ("01  R.\n  05  G.\n    10  C  PIC X(1).\n"
               "  05  G.\n    10  C  PIC 9(1).\n")
        cmp = compare(old, new)
        self.assertEqual(cmp.conclusion, UNVERIFIABLE)
        self.assertEqual(cmp.fields, [])      # matching would be a guess
        kinds = {p["kind"] for p in cmp.problems}
        self.assertEqual(kinds, {"duplicate_field_path"})
        paths = {p["path"] for p in cmp.problems}
        self.assertIn("R/G/C", paths)


if __name__ == "__main__":
    unittest.main()
