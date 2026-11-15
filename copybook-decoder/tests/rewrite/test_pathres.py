"""Path resolution: OCCURS indices, REDEFINES views, FILLER, groups."""

from __future__ import annotations

import unittest

from copybook.parser import parse_copybook
from copybook.rewrite.errors import RewriteOrderError
from copybook.rewrite.pathres import resolve_path
from tests.rewrite.fixtures import (LAYOUT_A, LAYOUT_B, LAYOUT_NESTED,
                                    LAYOUT_REDEF_LEN)


class BasicPathTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_A, codec="cp037")

    def test_root_children_without_root_name(self):
        r = resolve_path(self.layout, ["ORDER-ID"])
        self.assertEqual((r.start, r.end), (0, 8))
        self.assertEqual(r.path, ["ORDER-ID"])

    def test_names_are_upper_cased(self):
        r = resolve_path(self.layout, ["order-id"])
        self.assertEqual(r.field.name, "ORDER-ID")

    def test_occurs_group_requires_index(self):
        r = resolve_path(self.layout, ["ITEM-GROUP", 0, "ITEM-CODE"])
        self.assertEqual((r.start, r.end), (20, 24))
        r1 = resolve_path(self.layout, ["ITEM-GROUP", 1, "ITEM-CODE"])
        self.assertEqual((r1.start, r1.end), (28, 32))

    def test_occurs_amount_and_text_view(self):
        amt = resolve_path(self.layout,
                           ["ITEM-GROUP", 1, "ITEM-AMOUNT"])
        note = resolve_path(self.layout,
                            ["ITEM-GROUP", 1, "ITEM-NOTE"])
        # the REDEFINES text view covers exactly the same bytes
        self.assertEqual((amt.start, amt.end), (32, 36))
        self.assertEqual((note.start, note.end), (32, 36))

    def test_top_level_redefines_cluster(self):
        total = resolve_path(self.layout, ["TOTAL"])
        text = resolve_path(self.layout, ["TOTAL-TEXT"])
        self.assertEqual((total.start, total.end), (38, 43))
        self.assertEqual((text.start, text.end), (38, 43))

    def test_index_out_of_range(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP", 2, "ITEM-CODE"])

    def test_missing_index_is_an_error(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP", "ITEM-CODE"])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP"])

    def test_negative_and_bool_indices_rejected(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP", -1, "ITEM-CODE"])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP", True, "ITEM-CODE"])

    def test_unknown_name(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["NO-SUCH-FIELD"])

    def test_filler_group_and_array_not_writable(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["FILLER-1"])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP", 0])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["ITEM-GROUP"])

    def test_empty_and_non_array_paths(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, [])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, "ORDER-ID")
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, [True])


class NestedOccursTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_NESTED)

    def test_nested_indices_locate_distinct_offsets(self):
        # OUTER occurs 2, each 11 bytes: INNER 3 * (2+3) = 15 + OUTER-CODE 1
        # = 16 bytes per outer iteration.
        self.assertEqual(self.layout.record_length, 32)
        r00 = resolve_path(self.layout,
                           ["OUTER", 0, "INNER", 0, "CELL-CODE"])
        r02 = resolve_path(self.layout,
                           ["OUTER", 0, "INNER", 2, "CELL-CODE"])
        r10 = resolve_path(self.layout,
                           ["OUTER", 1, "INNER", 0, "CELL-CODE"])
        self.assertEqual((r00.start, r00.end), (0, 2))
        self.assertEqual((r02.start, r02.end), (10, 12))
        self.assertEqual((r10.start, r10.end), (16, 18))

    def test_same_named_leaf_needs_full_path(self):
        amt01 = resolve_path(self.layout,
                             ["OUTER", 0, "INNER", 1, "CELL-AMT"])
        amt11 = resolve_path(self.layout,
                             ["OUTER", 1, "INNER", 1, "CELL-AMT"])
        self.assertEqual((amt01.start, amt01.end), (7, 10))
        self.assertEqual((amt11.start, amt11.end), (23, 26))

    def test_every_occurs_layer_needs_an_index(self):
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout, ["OUTER", 0, "INNER", "CELL-CODE"])
        with self.assertRaises(RewriteOrderError):
            resolve_path(self.layout,
                         ["OUTER", 0, "INNER", 0, "CELL-CODE", 1])

    def test_outer_code_after_inner_array(self):
        r0 = resolve_path(self.layout, ["OUTER", 0, "OUTER-CODE"])
        r1 = resolve_path(self.layout, ["OUTER", 1, "OUTER-CODE"])
        self.assertEqual((r0.start, r0.end), (15, 16))
        self.assertEqual((r1.start, r1.end), (31, 32))


class RedefinesLengthTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_REDEF_LEN)

    def test_views_of_different_lengths(self):
        base = resolve_path(self.layout, ["BASE-FIELD"])
        short = resolve_path(self.layout, ["SHORT-VIEW"])
        long = resolve_path(self.layout, ["LONG-VIEW"])
        self.assertEqual((base.start, base.end), (0, 6))
        self.assertEqual((short.start, short.end), (0, 4))
        self.assertEqual((long.start, long.end), (0, 8))
        tail = resolve_path(self.layout, ["TAIL-FIELD"])
        # the longer view spills into the following field's bytes
        self.assertEqual((tail.start, tail.end), (8, 10))


class LayoutBPathTests(unittest.TestCase):
    def setUp(self):
        self.layout = parse_copybook(LAYOUT_B, codec="cp500")

    def test_batch_occurrences(self):
        r0 = resolve_path(self.layout,
                          ["BATCH-GROUP", 0, "BATCH-AMT"])
        r2 = resolve_path(self.layout,
                          ["BATCH-GROUP", 2, "BATCH-AMT"])
        self.assertEqual((r0.start, r0.end), (8, 14))
        self.assertEqual((r2.start, r2.end), (24, 30))

    def test_remark_redefines(self):
        remark = resolve_path(self.layout, ["REMARK"])
        num = resolve_path(self.layout, ["REMARK-NUM"])
        self.assertEqual((remark.start, remark.end), (33, 43))
        self.assertEqual((num.start, num.end), (33, 43))


if __name__ == "__main__":
    unittest.main()
