"""Tests for the deterministic masking primitives."""

import unittest
from datetime import date
from decimal import Decimal

from copybook.mask.mapping import (GIVEN_NAMES, FAMILY_NAMES,
                                   bucketize_amount, bucketize_date,
                                   pseudonym_value, shift_amount,
                                   shift_date, synthesize_name)

KEY = b"unit-test-key-material-0123456789"
OTHER_KEY = b"another-key-material-9876543210"


class TestPseudonym(unittest.TestCase):

    def test_deterministic(self):
        self.assertEqual(pseudonym_value(KEY, "g", "1000002345"),
                         pseudonym_value(KEY, "g", "1000002345"))

    def test_key_changes_result(self):
        self.assertNotEqual(pseudonym_value(KEY, "g", "1000002345"),
                            pseudonym_value(OTHER_KEY, "g", "1000002345"))

    def test_group_isolates(self):
        self.assertNotEqual(pseudonym_value(KEY, "g1", "1000002345"),
                            pseudonym_value(KEY, "g2", "1000002345"))

    def test_format_preserved_digits(self):
        fake = pseudonym_value(KEY, "g", "1000002345")
        self.assertEqual(len(fake), 10)
        self.assertTrue(fake.isdigit())

    def test_format_preserved_mixed(self):
        fake = pseudonym_value(KEY, "g", "AB-12-cd-34")
        self.assertEqual(len(fake), 11)
        self.assertEqual(fake[2], "-")
        self.assertEqual(fake[5], "-")
        self.assertEqual(fake[8], "-")
        self.assertTrue(fake[0].isupper() and fake[1].isupper())
        self.assertTrue(fake[3].isdigit() and fake[4].isdigit())
        self.assertTrue(fake[6].islower() and fake[7].islower())

    def test_no_leading_zero(self):
        for i in range(50):
            fake = pseudonym_value(KEY, "g", str(100000 + i),
                                   no_leading_zero=True)
            self.assertNotEqual(fake[0], "0")

    def test_long_values_use_extended_stream(self):
        text = "9" * 100
        fake = pseudonym_value(KEY, "g", text)
        self.assertEqual(len(fake), 100)
        self.assertTrue(fake.isdigit())
        self.assertNotEqual(fake, text)


class TestSynthesize(unittest.TestCase):

    def test_deterministic_and_keyed(self):
        a = synthesize_name(KEY, "s", "ZHANG WEI")
        self.assertEqual(a, synthesize_name(KEY, "s", "ZHANG WEI"))
        self.assertNotEqual(a, synthesize_name(OTHER_KEY, "s", "ZHANG WEI"))

    def test_no_fragment_of_original(self):
        originals = ["ZHANG WEI", "LI NA", "WANG FANG", "MARIA GARCIA",
                     "MUHAMMAD ALI", "AN", "BO"]
        for original in originals:
            fake = synthesize_name(KEY, "s", original)
            haystack = fake.casefold()
            for token in original.split():
                if len(token) >= 2:
                    self.assertNotIn(token.casefold(), haystack,
                                     f"{fake!r} leaks {token!r}")

    def test_uses_synthetic_pools(self):
        fake = synthesize_name(KEY, "s", "ZHANG WEI")
        given, family, seq = fake.split(" ")
        self.assertIn(given, GIVEN_NAMES)
        self.assertIn(family, FAMILY_NAMES)
        self.assertEqual(len(seq), 4)
        self.assertTrue(seq.isdigit())

    def test_scope_isolates(self):
        self.assertNotEqual(synthesize_name(KEY, "s1", "ZHANG WEI"),
                            synthesize_name(KEY, "s2", "ZHANG WEI"))


class TestShiftAmount(unittest.TestCase):

    def test_deterministic_within_bound(self):
        value = Decimal("120.50")
        got = shift_amount(KEY, "s", value, 2, max_pct=Decimal("5"))
        self.assertEqual(got, shift_amount(KEY, "s", value, 2,
                                           max_pct=Decimal("5")))
        self.assertLessEqual(abs(got - value), value * Decimal("0.05")
                             + Decimal("0.005"))
        self.assertEqual(-got.as_tuple().exponent, 2)

    def test_scale_preserved(self):
        got = shift_amount(KEY, "s", Decimal("1000000.00"), 2,
                           max_pct=Decimal("5"))
        self.assertEqual(-got.as_tuple().exponent, 2)
        got0 = shift_amount(KEY, "s", Decimal("1000"), 0,
                            max_pct=Decimal("10"))
        self.assertEqual(got0.as_tuple().exponent, 0)

    def test_negative_value(self):
        value = Decimal("-30.00")
        got = shift_amount(KEY, "s", value, 2, max_pct=Decimal("5"))
        self.assertLessEqual(abs(got - value), Decimal("1.505"))

    def test_max_abs_mode(self):
        value = Decimal("500.00")
        got = shift_amount(KEY, "s", value, 2, max_abs=Decimal("25"))
        self.assertLessEqual(abs(got - value), Decimal("25.005"))

    def test_exactly_one_bound_required(self):
        with self.assertRaises(ValueError):
            shift_amount(KEY, "s", Decimal("1"), 0)
        with self.assertRaises(ValueError):
            shift_amount(KEY, "s", Decimal("1"), 0,
                         max_pct=Decimal("1"), max_abs=Decimal("1"))

    def test_same_value_same_shift_other_scope_other_shift(self):
        value = Decimal("77.70")
        self.assertEqual(shift_amount(KEY, "s", value, 2,
                                      max_pct=Decimal("5")),
                         shift_amount(KEY, "s", value, 2,
                                      max_pct=Decimal("5")))
        self.assertNotEqual(shift_amount(KEY, "s1", value, 2,
                                         max_pct=Decimal("5")),
                            shift_amount(KEY, "s2", value, 2,
                                         max_pct=Decimal("5")))


class TestShiftDate(unittest.TestCase):

    def test_deterministic_within_bound(self):
        day = date(2024, 1, 15)
        got = shift_date(KEY, "s", day, "2024-01-15", max_days=30)
        self.assertEqual(got, shift_date(KEY, "s", day, "2024-01-15",
                                         max_days=30))
        self.assertLessEqual(abs((got - day).days), 30)

    def test_same_original_same_offset_across_calls(self):
        day = date(2024, 1, 15)
        offsets = {(shift_date(KEY, "s", day, "2024-01-15", max_days=30)
                    - day).days for _ in range(3)}
        self.assertEqual(len(offsets), 1)

    def test_different_dates_may_differ(self):
        results = {shift_date(KEY, "s", date(2024, 1, 1) +
                              __import__("datetime").timedelta(days=i),
                              "x", max_days=30)
                   for i in range(10)}
        self.assertGreater(len(results), 1)


class TestBucketize(unittest.TestCase):

    def test_amount_labels(self):
        bounds = (("1000.00", Decimal("1000.00")),
                  ("10000.00", Decimal("10000.00")))
        self.assertEqual(bucketize_amount(Decimal("999.99"), bounds),
                         "<1000.00")
        self.assertEqual(bucketize_amount(Decimal("1000.00"), bounds),
                         "1000.00-10000.00")
        self.assertEqual(bucketize_amount(Decimal("9999.99"), bounds),
                         "1000.00-10000.00")
        self.assertEqual(bucketize_amount(Decimal("10000.00"), bounds),
                         ">=10000.00")
        self.assertEqual(bucketize_amount(Decimal("-5"), bounds),
                         "<1000.00")

    def test_date_granularities(self):
        day = date(2024, 3, 15)
        self.assertEqual(bucketize_date(day, "month"), "2024-03")
        self.assertEqual(bucketize_date(day, "quarter"), "2024-Q1")
        self.assertEqual(bucketize_date(day, "year"), "2024")
        self.assertEqual(bucketize_date(date(2024, 12, 31), "quarter"),
                         "2024-Q4")
        with self.assertRaises(ValueError):
            bucketize_date(day, "week")


if __name__ == "__main__":
    unittest.main()
