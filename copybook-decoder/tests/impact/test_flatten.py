"""flatten_layout: full group paths, byte ranges, structural findings."""

import unittest

from copybook import parse_copybook
from copybook.impact import flatten_layout
from copybook.impact.flatten import pic_text


class TestPicText(unittest.TestCase):
    def test_rendering(self):
        self.assertEqual(pic_text("X", 10, False, 0), "X(10)")
        self.assertEqual(pic_text("9", 8, False, 0), "9(8)")
        self.assertEqual(pic_text("9", 9, True, 2), "S9(7)V99")
        self.assertEqual(pic_text("9", 9, True, 3), "S9(6)V999")
        self.assertEqual(pic_text("9", 1, False, 0), "9")


class TestFlatten(unittest.TestCase):
    def flatten(self, text, codec="cp037"):
        return flatten_layout(parse_copybook(text, name="T", version="1",
                                             codec=codec))

    def test_full_group_paths_and_absolute_offsets(self):
        flat = self.flatten("""\
            01  REC.
                05  HEAD.
                    10  ID      PIC X(4).
                    10  SEQ     PIC 9(2).
                05  BODY.
                    10  AMOUNT  PIC S9(5)V99 COMP-3.
        """)
        by_path = {leaf.path: leaf for leaf in flat.leaves}
        self.assertIn(("REC", "HEAD", "ID"), by_path)
        self.assertIn(("REC", "BODY", "AMOUNT"), by_path)
        amount = by_path[("REC", "BODY", "AMOUNT")]
        self.assertEqual((amount.offset, amount.end), (6, 10))  # 4+2, 4B
        self.assertEqual(amount.path_text, "REC/BODY/AMOUNT")
        self.assertEqual(flat.record_length, 10)

    def test_occurs_attributes(self):
        flat = self.flatten("""\
            01  REC.
                05  ITEM      PIC X(3) OCCURS 4 TIMES.
                05  GRP       OCCURS 2 TIMES.
                    10  CODE  PIC 9(2).
        """)
        by_path = {leaf.path: leaf for leaf in flat.leaves}
        item = by_path[("REC", "ITEM")]
        self.assertEqual((item.offset, item.byte_length, item.occurs,
                          item.total_length), (0, 3, 4, 12))
        code = by_path[("REC", "GRP", "CODE")]
        self.assertEqual((code.offset, code.occurs, code.end), (12, 1, 14))
        self.assertEqual(flat.record_length, 16)

    def test_redefines_view_cluster_overlays_target(self):
        flat = self.flatten("""\
            01  REC.
                05  CODE      PIC X(4).
                05  CODE-NUM  REDEFINES CODE PIC 9(4).
                05  CODE-HEX  REDEFINES CODE PIC X(4).
                05  TAIL      PIC X(1).
        """)
        self.assertEqual(flat.record_length, 5)  # views add no storage
        self.assertEqual(len(flat.view_clusters), 1)
        cluster = flat.view_clusters[0]
        self.assertEqual(cluster.target_path, ("REC", "CODE"))
        self.assertEqual((cluster.byte_start, cluster.byte_end), (0, 4))
        self.assertEqual(cluster.views,
                         (("REC", "CODE-NUM"), ("REC", "CODE-HEX")))
        view = next(leaf for leaf in flat.leaves if leaf.name == "CODE-NUM")
        self.assertEqual(view.redefines, ("REC", "CODE"))
        self.assertEqual((view.offset, view.end), (0, 4))

    def test_view_over_a_whole_group(self):
        flat = self.flatten("""\
            01  REC.
                05  GRP.
                    10  A   PIC X(2).
                    10  B   PIC 9(2).
                05  RAW       REDEFINES GRP PIC X(4).
        """)
        self.assertEqual(flat.record_length, 4)
        (cluster,) = flat.view_clusters
        self.assertEqual(cluster.target_path, ("REC", "GRP"))
        self.assertEqual((cluster.byte_start, cluster.byte_end), (0, 4))
        self.assertEqual(cluster.views, (("REC", "RAW"),))

    def test_duplicate_group_path_detected(self):
        flat = self.flatten("""\
            01  REC.
                05  GRP.
                    10  CODE  PIC X(2).
                05  GRP.
                    10  CODE  PIC 9(2).
        """)
        paths = {dup.path for dup in flat.duplicate_paths}
        self.assertIn(("REC", "GRP"), paths)
        self.assertIn(("REC", "GRP", "CODE"), paths)
        self.assertEqual(flat.leaf_map(), {})   # ambiguous -> no matching

    def test_same_name_in_different_groups_is_not_a_duplicate(self):
        flat = self.flatten("""\
            01  REC.
                05  GRP-A.
                    10  CODE  PIC X(2).
                05  GRP-B.
                    10  CODE  PIC 9(2).
        """)
        self.assertEqual(flat.duplicate_paths, [])
        self.assertEqual({"/".join(p) for p in flat.shared_names["CODE"]},
                         {"REC/GRP-A/CODE", "REC/GRP-B/CODE"})
        self.assertEqual(len(flat.leaf_map()), 2)

    def test_filler_intervals_and_exclusion_from_matching(self):
        flat = self.flatten("""\
            01  REC.
                05  A       PIC X(2).
                05  FILLER  PIC X(3).
                05  FILLER  PIC X(1).
        """)
        self.assertEqual(flat.filler_intervals, [(2, 5), (5, 6)])
        self.assertEqual([leaf.name for leaf in flat.named_leaves], ["A"])

    def test_codec_recorded_per_leaf(self):
        flat = self.flatten("01  REC.\n  05  A  PIC X(2).\n",
                            codec="cp500")
        self.assertEqual(flat.leaves[0].codec, "cp500")


if __name__ == "__main__":
    unittest.main()
