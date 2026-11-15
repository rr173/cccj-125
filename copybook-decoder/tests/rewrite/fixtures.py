"""Copybooks and hand-built byte fixtures for rewrite tests.

Two layouts are used so mixed-layout originals can be exercised:

* ``ORD-A`` (cp037), 44 bytes, with two fixed OCCURS groups, an
  in-group REDEFINES, a non-zero FILLER and a second REDEFINES cluster;
* ``SUM-B`` (cp500), 50 bytes, with a nested OCCURS group, a FILLER and
  a signed zoned field.

All expected hex strings in the tests are computed by hand (or by an
independent reference builder in this module that does not import the
production encoder).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

LAYOUT_A = """\
       01  ORDER-RECORD.
           05  ORDER-ID          PIC X(8).
           05  CUSTOMER-NAME     PIC X(12).
           05  ITEM-GROUP OCCURS 2 TIMES.
               10  ITEM-CODE      PIC X(4).
               10  ITEM-AMOUNT    PIC S9(5)V99 COMP-3.
               10  ITEM-NOTE      REDEFINES ITEM-AMOUNT PIC X(4).
           05  FILLER             PIC X(2).
           05  TOTAL              PIC S9(7)V99 COMP-3.
           05  TOTAL-TEXT         REDEFINES TOTAL PIC X(5).
           05  FLAG               PIC 9(1).
"""

LAYOUT_B = """\
       01  SUMMARY-RECORD.
           05  SUMMARY-ID      PIC X(6).
           05  BATCH-GROUP OCCURS 3 TIMES.
               10  BATCH-CODE   PIC X(2).
               10  BATCH-AMT    PIC S9(9)V99 COMP-3.
           05  FILLER          PIC X(3).
           05  REMARK          PIC X(10).
           05  REMARK-NUM      REDEFINES REMARK PIC 9(10).
           05  ZONED-AMT       PIC S9(5)V99.
"""

# A layout dedicated to nested OCCURS with same-named leaves.
LAYOUT_NESTED = """\
       01  NESTED-RECORD.
           05  OUTER OCCURS 2 TIMES.
               10  INNER OCCURS 3 TIMES.
                   15  CELL-CODE PIC X(2).
                   15  CELL-AMT  PIC 9(3).
               10  OUTER-CODE PIC X(1).
"""

# REDEFINES views with lengths different from the base field.
LAYOUT_REDEF_LEN = """\
       01  RL-RECORD.
           05  BASE-FIELD   PIC X(6).
           05  SHORT-VIEW   REDEFINES BASE-FIELD PIC 9(4).
           05  LONG-VIEW    REDEFINES BASE-FIELD PIC X(8).
           05  TAIL-FIELD   PIC X(2).
           05  FILLER       PIC X(2).
"""

# Wide numeric pictures for the 60-digit precision checks.
LAYOUT_WIDE = """\
       01  WIDE-RECORD.
           05  WIDE-DISP      PIC 9(60).
           05  WIDE-DISP-S    PIC S9(60).
           05  WIDE-PACK      PIC 9(60) COMP-3.
           05  WIDE-PACK-S    PIC S9(60) COMP-3.
           05  SCALED         PIC S9(7)V99 COMP-3.
           05  SCALED-ZONED   PIC S9(5)V99.
           05  TEXT-FIELD     PIC X(10).
"""


def write_layouts(base: Path) -> dict[str, Path]:
    base.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, text in (("ORDA.cpy", LAYOUT_A), ("SUMB.cpy", LAYOUT_B),
                       ("NESTED.cpy", LAYOUT_NESTED),
                       ("RL.cpy", LAYOUT_REDEF_LEN),
                       ("WIDE.cpy", LAYOUT_WIDE)):
        p = base / name
        p.write_text(text, encoding="utf-8")
        paths[name] = p
    return paths


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------- reference bytes

def ref_zoned(value: str, length: int, signed: bool, codec: str = "cp037",
              scale: int = 0):
    """Independent zoned encoder used only by the tests.

    Scales the decimal string to an exact integer of ``length`` digits;
    fraction digits are zero-padded on the right.
    """
    negative = value.startswith("-")
    body = value[1:] if negative else value
    if "." in body:
        int_part, frac = body.split(".")
    else:
        int_part, frac = body, ""
    if len(frac) > scale:
        raise ValueError("too many fraction digits for the stated scale")
    digits = (int_part + frac + "0" * (scale - len(frac))).rjust(
        length, "0")
    assert len(digits) == length
    raw = bytearray()
    for d in digits[:-1]:
        raw.append(0xF0 | int(d))
    last = int(digits[-1])
    if signed:
        zone = 0xD0 if negative else 0xC0
    else:
        zone = 0xF0
    raw.append(zone | last)
    return bytes(raw)


def ref_comp3(value: str, num_digits: int, signed: bool, scale: int = 0):
    """Independent COMP-3 encoder used only by the tests."""
    negative = value.startswith("-")
    body = value[1:] if negative else value
    if "." in body:
        int_part, frac = body.split(".")
    else:
        int_part, frac = body, ""
    if len(frac) > scale:
        raise ValueError("too many fraction digits for the stated scale")
    scaled = int_part + frac + "0" * (scale - len(frac))
    digits = scaled.rjust(num_digits, "0")
    assert len(digits) == num_digits
    sign = (0xF if not signed else 0xD if negative else 0xC)
    nibbles = [int(d) for d in digits] + [sign]
    if len(nibbles) % 2:
        nibbles = [0] + nibbles
    return bytes((nibbles[i] << 4) | nibbles[i + 1]
                 for i in range(0, len(nibbles), 2))


def ref_text(value: str, width: int, codec: str):
    raw = value.encode(codec)
    assert len(raw) <= width
    return raw + " ".encode(codec) * (width - len(raw))


# ----------------------------------------------------------- demo original

def build_demo_records() -> tuple[bytes, bytes]:
    """Hand-built mixed-layout records (see module docstring).

    Record A embeds a literal 0x0A inside a COMP-3 field and a
    non-canonical positive sign nibble (A); both must survive untouched.
    """
    a = bytearray(44)
    a[0:8] = "ORD00001".encode("cp037")
    a[8:20] = ref_text("ACME CORP", 12, "cp037")
    a[20:24] = "I001".encode("cp037")
    a[24:28] = ref_comp3("123.45", 7, True, 2)      # 00 12 34 5c
    a[28:32] = "I002".encode("cp037")
    a[32:36] = ref_comp3("-7.50", 7, True, 2)       # 00 00 07 5d
    a[36:38] = bytes([0x40, 0xFF])               # non-zero FILLER
    # TOTAL 9087.00 with non-canonical positive sign nibble A and an 0x0A
    a[38:43] = bytes([0x00, 0x09, 0x0A, 0x87, 0x0A])
    a[43:44] = "7".encode("cp037")

    b = bytearray(50)
    b[0:6] = "SUM001".encode("cp500")               # SUMMARY-ID X(6)
    b[6:8] = "B1".encode("cp500")
    b[8:14] = ref_comp3("100.00", 11, True, 2)
    b[14:16] = "B2".encode("cp500")
    b[16:22] = ref_comp3("-250.25", 11, True, 2)
    b[22:24] = "B3".encode("cp500")
    b[24:30] = ref_comp3("0.00", 11, True, 2)
    b[30:33] = bytes([0x5A, 0x7B, 0x40])         # non-zero FILLER
    b[33:43] = ref_text("HELLO", 10, "cp500")
    b[43:50] = ref_zoned("-12.34", 7, True, "cp500", 2)
    return bytes(a), bytes(b)


# Layout of the assembled demo file (lengths in bytes).
DEMO_PREAMBLE = b"HEAD\x00\x01"                 # 6
DEMO_GAP = b"\n\x00FEEDER-BYTES"               # 14, includes a 0x0A
DEMO_TRAILER = b"TAIL-END\x1a"                  # 9


def build_demo_file() -> tuple[bytes, dict]:
    """Assemble the full demo original with bytes preserved between records."""
    record_a, record_b = build_demo_records()
    a_start = len(DEMO_PREAMBLE)
    a_end = a_start + len(record_a)
    b_start = a_end + len(DEMO_GAP)
    b_end = b_start + len(record_b)
    whole = (DEMO_PREAMBLE + record_a + DEMO_GAP + record_b + DEMO_TRAILER)
    spans = {"A": (a_start, a_end), "B": (b_start, b_end)}
    return whole, spans
