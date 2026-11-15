"""Exact decimal, date and timestamp parsing for settlement events.

All money is kept as integer *cents* (1/100 of a currency unit) using
Python's unbounded ``int``: amounts beyond 64 bits are supported and no
``float`` is ever involved.  Amount strings are strict two-decimal-place
positive decimals; exponents, ``NaN``, implicit rounding and zero are
refused rather than tolerated.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# Digits, optional single dot, exactly two fractional digits. No sign,
# no exponent, no thousands separators, no whitespace. Leading zeros on
# the integer part are allowed ("007" stays "007" -- ids are never parsed
# numerically anyway), but the total value must be strictly positive.
_AMOUNT_RE = re.compile(r"^[0-9]+\.[0-9]{2}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# ISO 8601 second-level timestamp with an explicit UTC offset. Fractional
# seconds are deliberately rejected (the feed is second-level). The
# trailing "Z" form is accepted as the explicit zero offset.
_TS_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$")


def parse_cents(raw) -> int:
    """Parse a strict positive two-decimal string into integer cents.

    Raises ``ValueError`` for anything that is not exactly
    ``"<digits>.<two digits>"`` or whose value is zero.
    """
    if not isinstance(raw, str) or not _AMOUNT_RE.match(raw):
        raise ValueError(
            f"amount must be a positive decimal string with exactly two "
            f"fractional digits (no exponent, no rounding), got {raw!r}")
    whole, frac = raw.split(".", 1)
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        raise ValueError(f"amount must be strictly positive, got {raw!r}")
    return cents


def format_cents(cents: int) -> str:
    """Render integer cents back as an exact two-decimal string."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def parse_due_date(raw):
    """Parse a strict ``YYYY-MM-DD`` calendar date (rejects 2024-02-30)."""
    if not isinstance(raw, str) or not _DATE_RE.match(raw):
        raise ValueError(
            f"due_on must be a YYYY-MM-DD string, got {raw!r}")
    try:
        return date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    except ValueError as exc:
        raise ValueError(f"due_on {raw!r} is not a valid calendar date: {exc}")


def parse_timestamp(raw):
    """Parse a second-level ISO 8601 timestamp carrying an explicit offset.

    Returns a timezone-aware :class:`~datetime.datetime` converted to UTC.
    Naive timestamps (no offset), fractional seconds and non-string inputs
    are rejected.
    """
    if not isinstance(raw, str) or not _TS_RE.match(raw):
        raise ValueError(
            "at must be an ISO 8601 second-level timestamp with an explicit "
            f"UTC offset (e.g. 2026-01-31T08:00:00+08:00), got {raw!r}")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"timestamp {raw!r} is not a valid calendar time: "
                         f"{exc}")
    # fromisoformat only returns a naive value when there is no offset;
    # the regex guarantees one, but keep the guard explicit.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"timestamp {raw!r} must carry an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def utc_calendar_day(moment: datetime) -> date:
    """Return the UTC calendar date used as the aging base day."""
    return moment.astimezone(timezone.utc).date()


def age_in_days(base_day: date, due_on: date) -> int:
    """Whole-day difference ``base_day - due_on`` (may be negative)."""
    return (base_day - due_on) // timedelta(days=1)


def age_bucket(age_days: int) -> str:
    """Map an integer day difference to the fixed aging bucket code."""
    if age_days <= 0:
        return "not_due"
    if age_days <= 30:
        return "overdue_1_30"
    if age_days <= 60:
        return "overdue_31_60"
    if age_days <= 90:
        return "overdue_61_90"
    return "overdue_91_plus"


AGING_BUCKETS = ("not_due", "overdue_1_30", "overdue_31_60",
                 "overdue_61_90", "overdue_91_plus")


def require_nonempty_text(raw, label: str) -> Optional[str]:
    """Return *raw* when it is a non-empty string, else raise."""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be a non-empty string, got {raw!r}")
    return raw
