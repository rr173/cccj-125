"""Unit tests for exact money, date and timestamp parsing."""

import unittest
from datetime import date

from copybook.settle import (age_bucket, age_in_days, format_cents,
                             parse_cents, parse_due_date, parse_timestamp)
from copybook.settle.values import utc_calendar_day


class MoneyTests(unittest.TestCase):
    def test_two_decimal_positive_strings(self):
        self.assertEqual(parse_cents("0.01"), 1)
        self.assertEqual(parse_cents("100.00"), 10000)
        self.assertEqual(parse_cents("1234567.89"), 123456789)

    def test_amount_beyond_64_bit_is_exact(self):
        # 10^40 yuan in cents: far beyond a signed 64-bit integer.
        huge = "1" + "0" * 40 + ".00"
        cents = parse_cents(huge)
        self.assertEqual(cents, 10 ** 42)
        self.assertEqual(format_cents(cents), huge)

    def test_illegal_amounts(self):
        for bad in ("0", "0.00", "100", "100.0", "100.000", "-1.00",
                    "1e3", "1E3", "NaN", "Infinity", ".50", "1.2.3",
                    " 1.00", "1.00 ", "+1.00", 100, None, "", "abc.00"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_cents(bad)

    def test_format_round_trip(self):
        for text in ("0.01", "99999999999999999999.99", "7.30"):
            self.assertEqual(format_cents(parse_cents(text)), text)


class DateAndTimestampTests(unittest.TestCase):
    def test_due_date_strict_calendar(self):
        self.assertEqual(parse_due_date("2026-02-28"), date(2026, 2, 28))
        for bad in ("2026-2-28", "2026/02/28", "20260228", "2026-02-30",
                    "2026-13-01", "not-a-date", None, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_due_date(bad)

    def test_timestamp_requires_explicit_offset(self):
        moment = parse_timestamp("2026-01-31T08:00:00+08:00")
        self.assertEqual(moment.isoformat(), "2026-01-31T00:00:00+00:00")
        self.assertEqual(
            parse_timestamp("2026-01-31T00:00:00Z").isoformat(),
            "2026-01-31T00:00:00+00:00")
        self.assertEqual(
            parse_timestamp("2026-01-31T02:30:00-05:00").isoformat(),
            "2026-01-31T07:30:00+00:00")

    def test_illegal_timestamps(self):
        for bad in ("2026-01-31T00:00:00", "2026-01-31 00:00:00Z",
                    "2026-01-31T00:00:00.5Z", "2026-01-31",
                    "2026-01-31T25:00:00Z", None, "", "now"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_timestamp(bad)

    def test_utc_calendar_day_uses_utc_not_local(self):
        # 00:30 in +08:00 is still the previous UTC calendar day.
        moment = parse_timestamp("2026-02-01T00:30:00+08:00")
        self.assertEqual(utc_calendar_day(moment), date(2026, 1, 31))


class AgingTests(unittest.TestCase):
    BASE = date(2026, 2, 1)

    def test_age_days_integer_difference(self):
        self.assertEqual(age_in_days(self.BASE, date(2026, 2, 1)), 0)
        self.assertEqual(age_in_days(self.BASE, date(2026, 2, 2)), -1)
        self.assertEqual(age_in_days(self.BASE, date(2026, 1, 31)), 1)
        self.assertEqual(age_in_days(self.BASE, date(2025, 11, 3)), 90)

    def test_bucket_boundaries(self):
        cases = {
            # not_due: age <= 0 (due today or in the future)
            -5: "not_due", 0: "not_due",
            1: "overdue_1_30", 30: "overdue_1_30",
            31: "overdue_31_60", 60: "overdue_31_60",
            61: "overdue_61_90", 90: "overdue_61_90",
            91: "overdue_91_plus", 400: "overdue_91_plus",
        }
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(age_bucket(days), expected)


if __name__ == "__main__":
    unittest.main()
