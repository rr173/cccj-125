"""Deterministic, key-derived masking primitives.

Every masking function here is a *pure* function of

* the caller-supplied mapping key (never persisted anywhere),
* the algorithm domain separator (``copybook-mask/v1|<action>``),
* the linkage scope (a policy ``group`` name, or the full field path when
  no group is declared), and
* the original value itself.

Nothing depends on input file order, record order or chunk size, so the
same original always produces the same masked value no matter how the
input is sliced.  All derivations use HMAC-SHA256 in counter mode; without
the key the original value cannot be recovered from the masked one.

The key itself is supplied by the caller from an environment variable or a
file.  Only its SHA-256 fingerprint (16 hex chars) may appear in reports;
the key material is never written to outputs, diagnostics, logs or
reports, and no reversible original->fake mapping table is persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Optional, Union

from .errors import MaskError, MaskKeyError

#: Domain separator / algorithm version.  Bumping this changes every
#: derived value, so it is part of the stable-mapping input scope.
DOMAIN = "copybook-mask/v1"

#: Minimum accepted key material, in bytes.
MIN_KEY_BYTES = 16

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{16}$")


# --------------------------------------------------------------------- key

def key_fingerprint(material: bytes) -> str:
    """One-way fingerprint safe to record in reports."""
    return "sha256:" + hashlib.sha256(material).hexdigest()[:16]


@dataclass(frozen=True)
class MaskKey:
    """Caller-supplied mapping key.

    The material lives only in memory for the duration of the run.  The
    fingerprint is the only key-derived value that may be reported.
    """

    material: bytes
    fingerprint: str

    @classmethod
    def _from_text(cls, text: str, source: str) -> "MaskKey":
        material = text.encode("utf-8")
        if len(material) < MIN_KEY_BYTES:
            raise MaskKeyError(
                f"mapping key from {source} is only {len(material)} "
                f"byte(s); at least {MIN_KEY_BYTES} are required")
        return cls(material, key_fingerprint(material))

    @classmethod
    def from_env(cls, var_name: str) -> "MaskKey":
        """Load the key from an environment variable."""
        if not var_name:
            raise MaskKeyError("environment variable name must not be empty")
        raw = os.environ.get(var_name)
        if raw is None:
            raise MaskKeyError(
                f"mapping key not available: environment variable "
                f"{var_name} is not set")
        return cls._from_text(raw.strip(), f"environment variable {var_name}")

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "MaskKey":
        """Load the key from a file (trailing newline tolerated)."""
        p = Path(path)
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise MaskKeyError(f"mapping key not available: cannot read "
                               f"key file {p}: {exc}")
        return cls._from_text(raw.strip(), f"key file {p}")

    def check_fingerprint(self, declared: Optional[str],
                          policy_source: str = "policy") -> None:
        """Verify the key against the fingerprint declared by the policy."""
        if declared is None:
            return
        if not _FINGERPRINT_RE.match(declared):
            raise MaskKeyError(
                f"policy declares a malformed key_fingerprint {declared!r}; "
                "expected 'sha256:' followed by 16 hex characters")
        if declared != self.fingerprint:
            raise MaskKeyError(
                f"mapping key does not match the key_fingerprint declared "
                f"by {policy_source}; refusing to mask with the wrong key")


# --------------------------------------------------------- byte derivation

def _keystream(key: bytes, msg: bytes):
    """Yield an unbounded deterministic byte stream (HMAC counter mode)."""
    counter = 0
    while True:
        block = hmac.new(key, msg + b"#" + str(counter).encode("ascii"),
                         hashlib.sha256).digest()
        yield from block
        counter += 1


def _draw_char(stream, alphabet: str) -> str:
    """Draw one character uniformly from *alphabet* (rejection sampling)."""
    n = len(alphabet)
    limit = (256 // n) * n
    for byte in stream:
        if byte < limit:
            return alphabet[byte % n]
    raise AssertionError("unreachable")  # pragma: no cover


def _draw_int(key: bytes, msg: bytes, n: int) -> int:
    """Uniform integer in [0, n) via rejection sampling on 8-byte blocks."""
    if n <= 0:
        raise ValueError("n must be positive")
    limit = ((1 << 64) // n) * n
    counter = 0
    while True:
        digest = hmac.new(key, msg + b"#" + str(counter).encode("ascii"),
                          hashlib.sha256).digest()
        value = int.from_bytes(digest[:8], "big")
        if value < limit:
            return value % n
        counter += 1


# --------------------------------------------------------------- pseudonym

_ALPHABETS = {
    "digit": "0123456789",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "lower": "abcdefghijklmnopqrstuvwxyz",
}
_NO_LEADING_ZERO = "123456789"


def _char_class(ch: str) -> Optional[str]:
    if "0" <= ch <= "9":
        return "digit"
    if "A" <= ch <= "Z":
        return "upper"
    if "a" <= ch <= "z":
        return "lower"
    return None


def pseudonym_value(key: bytes, group: str, text: str,
                    no_leading_zero: bool = False) -> str:
    """Map *text* to a stable, format-preserving fake inside *group*.

    Each character keeps its class: digits become digits, uppercase stays
    uppercase, lowercase stays lowercase; anything else (separators,
    spaces) is kept verbatim.  The mapping is injective as far as the
    output alphabet allows -- the engine verifies that per group and
    refuses to deliver if two distinct originals collapse.
    """
    msg = f"{DOMAIN}|pseudonym|{group}|{text}".encode("utf-8")
    stream = _keystream(key, msg)
    out = []
    for index, ch in enumerate(text):
        cls = _char_class(ch)
        if cls is None:
            out.append(ch)
        elif index == 0 and no_leading_zero and cls == "digit":
            out.append(_draw_char(stream, _NO_LEADING_ZERO))
        else:
            out.append(_draw_char(stream, _ALPHABETS[cls]))
    return "".join(out)


# -------------------------------------------------------------- synthesize

# Invented name pools.  These tokens are synthetic by construction; the
# fragment check below additionally guarantees no piece of the original
# text leaks into the replacement.
GIVEN_NAMES = (
    "MIRA", "ALDEN", "TAVIA", "ORIN", "SELKIE", "BRANN", "CATO", "DELIA",
    "EMRIC", "FABLE", "SORREL", "PIKE", "NOVA", "HALDEN", "ISOLDE", "JORY",
    "KESTREL", "LIOR", "MAVEN", "NIALA", "OSTEN", "PEREN", "QUILLA",
    "ROAN", "SABLE", "TAMSIN", "ULRIC", "VESNA", "WREN", "YARDEN",
    "ZEPHIR", "ONDRA",
)
FAMILY_NAMES = (
    "HOLT", "FERRAN", "QUILL", "MARLOW", "ROOK", "HALVARD", "SORREL",
    "PIKE", "NORRIS", "TALBOT", "VERROW", "ASHDOWN", "BRISCO", "CARN",
    "DELVEC", "ELSTOW", "FENWICK", "GARROW", "HARN", "INKWELL", "JESSOP",
    "KELD", "LARKSPUR", "MERE", "NACKER", "OSGOOD", "PERNELL", "QUARLES",
    "RUSHFEN", "SKELTON", "THORNCROFT", "UMBER",
)

_FRAGMENT_RE = re.compile(r"[^0-9A-Za-z]+")


def _has_fragment(original: str, candidate: str) -> bool:
    """True when *candidate* contains any recognisable piece of *original*.

    Every alphanumeric token of the original with length >= 2, plus the
    whole original string, must be absent (case-insensitive).
    """
    haystack = candidate.casefold()
    whole = original.strip().casefold()
    if len(whole) >= 2 and whole in haystack:
        return True
    for token in _FRAGMENT_RE.split(original):
        if len(token) >= 2 and token.casefold() in haystack:
            return True
    return False


def synthesize_name(key: bytes, scope: str, original: str) -> str:
    """Replace free text with a synthetic value holding no original part.

    Deterministic per (key, scope, original); a retry counter re-rolls the
    derivation in the unlikely case a candidate contains a fragment of the
    original, so the no-fragment guarantee always holds.
    """
    for attempt in range(1000):
        msg = (f"{DOMAIN}|synthesize|{scope}|{original}|{attempt}"
               .encode("utf-8"))
        digest = hmac.new(key, msg, hashlib.sha256).digest()
        given = GIVEN_NAMES[digest[0] % len(GIVEN_NAMES)]
        family = FAMILY_NAMES[digest[1] % len(FAMILY_NAMES)]
        seq = int.from_bytes(digest[2:6], "big") % 10000
        candidate = f"{given} {family} {seq:04d}"
        if not _has_fragment(original, candidate):
            return candidate
    raise MaskError(  # pragma: no cover - practically unreachable
        "could not synthesize a fragment-free value after 1000 attempts")


# ------------------------------------------------------------------ amount

def _round_half_away_from_zero(x: Fraction) -> int:
    """Exact ROUND_HALF_UP (away from zero on ties) for a rational."""
    if x >= 0:
        return math.floor(x + Fraction(1, 2))
    return -math.floor(-x + Fraction(1, 2))


def shift_amount(key: bytes, scope: str, value: Decimal, scale: int, *,
                 max_pct: Optional[Decimal] = None,
                 max_abs: Optional[Decimal] = None) -> Decimal:
    """Deterministically jitter an amount, preserving its decimal scale.

    The delta is derived from HMAC(key, scope, original) and lies in
    +/- *max_pct* percent of the value, or in +/- *max_abs* absolute
    units.  All arithmetic is exact rational arithmetic (no decimal
    context involved), then rounded half-up -- away from zero on ties --
    to the original scale, so any independent re-implementation of this
    specification reproduces the identical result.
    """
    if (max_pct is None) == (max_abs is None):
        raise ValueError("exactly one of max_pct / max_abs is required")
    msg = f"{DOMAIN}|shift-amount|{scope}|{value}".encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    num = int.from_bytes(digest[:8], "big")
    frac = Fraction(2 * num - (1 << 64), 1 << 64)      # in [-1, 1), exact
    if max_pct is not None:
        delta = Fraction(value) * Fraction(max_pct) / 100 * frac
    else:
        delta = Fraction(max_abs) * frac
    scaled = (Fraction(value) + delta) * (10 ** scale)
    rounded = _round_half_away_from_zero(scaled)
    digits = tuple(int(d) for d in str(abs(rounded)))
    return Decimal((1 if rounded < 0 else 0, digits, -scale))


def bucketize_amount(value: Decimal,
                     boundaries: tuple) -> str:
    """Generalise an amount to a range label.

    *boundaries* is a tuple of (label_text, Decimal) pairs, strictly
    increasing.  Labels: ``<b0``, ``b_i-b_{i+1}`` (half-open [lo, hi)),
    ``>=b_n``.
    """
    for i, (text, bound) in enumerate(boundaries):
        if value < bound:
            if i == 0:
                return f"<{text}"
            prev_text = boundaries[i - 1][0]
            return f"{prev_text}-{text}"
    return f">={boundaries[-1][0]}"


# -------------------------------------------------------------------- date

def shift_date(key: bytes, scope: str, day: date, original_text: str, *,
               max_days: int) -> date:
    """Shift a date by a deterministic offset in +/- *max_days* days.

    The offset depends on (key, scope, original text), so the same
    original date shifts identically in every file sharing the scope --
    records of one subject stay aligned -- while different dates receive
    independent offsets.
    """
    msg = (f"{DOMAIN}|shift-date|{scope}|{original_text}".encode("utf-8"))
    offset = _draw_int(key, msg, 2 * max_days + 1) - max_days
    return day + timedelta(days=offset)


def bucketize_date(day: date, granularity: str) -> str:
    """Generalise a date to month / quarter / year."""
    if granularity == "month":
        return f"{day.year:04d}-{day.month:02d}"
    if granularity == "quarter":
        return f"{day.year:04d}-Q{(day.month - 1) // 3 + 1}"
    if granularity == "year":
        return f"{day.year:04d}"
    raise ValueError(f"unknown granularity {granularity!r}")
