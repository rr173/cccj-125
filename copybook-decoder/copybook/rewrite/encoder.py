"""Canonical byte encoding of corrected field values.

Each new value is encoded into exactly the byte capacity the existing
layout assigns to the field.  Encoding is canonical: one value always
produces the same bytes, and fields the correction order does not touch
keep whatever (possibly non-standard but legal) byte representation they
already had.

Capacity rules
--------------
* ``PIC X`` / ``PIC A`` accept a *string*, convert it through the
  layout's single-byte codec with ``errors='strict'`` (no replacement of
  un-encodable characters, no implicit ``str()`` conversion), and right
  pad with the codec's space byte.  Over-long text is rejected, never
  truncated.  The empty string becomes an all-space field.
* ``PIC 9`` / ``PIC S9`` (optionally with ``V``) accept only a decimal
  string matching ``-?[0-9]+(?:\\.[0-9]+)?``: no plus sign, exponent,
  whitespace, thousands separator or float.  Fraction digits may be
  fewer than the scale (right zero-padded) but never more -- extra
  trailing zeroes are still an overflow.  After exact scaling the
  absolute integer must fit the PICTURE's digit count.
* Unsigned PICTUREs reject every negative input, including ``-0``.
  Signed negative zero is written as non-negative zero.
* Zoned (DISPLAY) numerics use plain ``F``-zone digits, with the sign
  overpunched on the last byte (``C`` zone positive, ``D`` zone
  negative).  Packed (COMP-3) numerics use leading zero padding nibbles
  and a final sign nibble (``C`` / ``D`` signed, ``F`` unsigned).

All arithmetic is done on Python integers and strings, which have no
fixed precision ceiling; the 28-digit default of :class:`decimal.Decimal`
is never involved, so 60-digit values are handled exactly.
"""

from __future__ import annotations

import re

from ..model import Field
from .errors import RewriteValueError

_DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_ZONED_DIGIT = 0xF0          # EBCDIC '0'
_ZONED_POSITIVE_ZONE = 0xC0  # last byte zone for a positive overpunch
_ZONED_NEGATIVE_ZONE = 0xD0  # last byte zone for a negative overpunch
_COMP3_SIGN_POSITIVE = 0xC
_COMP3_SIGN_NEGATIVE = 0xD
_COMP3_SIGN_UNSIGNED = 0xF


def encode_value(value, field: Field, codec: str) -> bytes:
    """Encode *value* into the exact byte span of elementary *field*."""
    assert field.pic is not None
    if field.pic.category == "X":
        if not isinstance(value, str):
            raise RewriteValueError(
                f"field {field.name}: PIC X/A accepts only a JSON string, "
                f"got {type(value).__name__}")
        return _encode_text(value, field, codec)
    if not isinstance(value, str):
        raise RewriteValueError(
            f"field {field.name}: numeric PIC accepts only a decimal "
            f"string, got {type(value).__name__} (floats are rejected)")
    return _encode_number(value, field)


# ------------------------------------------------------------------- text

def _encode_text(value: str, field: Field, codec: str) -> bytes:
    try:
        encoded = value.encode(codec, errors="strict")
    except UnicodeEncodeError as exc:
        raise RewriteValueError(
            f"field {field.name}: character cannot be encoded with "
            f"{codec!r}: {exc}") from exc
    width = field.byte_length
    if len(encoded) > width:
        raise RewriteValueError(
            f"field {field.name}: value encodes to {len(encoded)} bytes "
            f"but the field holds only {width}; truncation is not allowed")
    space = " ".encode(codec)  # single-byte codec -> exactly one byte
    return encoded + space * (width - len(encoded))


# ---------------------------------------------------------------- numeric

def _scaled_integer(value: str, field: Field) -> tuple[int, bool]:
    """Return (exact scaled integer, negative flag), validating the string."""
    assert field.pic is not None
    if not _DECIMAL_RE.match(value):
        raise RewriteValueError(
            f"field {field.name}: {value!r} is not a legal decimal string "
            "(expected -?[0-9]+(?:.[0-9]+)?; no sign-plus, exponent, "
            "whitespace, thousands separator or float)")
    negative = value.startswith("-")
    if negative:
        body = value[1:]
    else:
        body = value
    if "." in body:
        int_part, frac_part = body.split(".", 1)
    else:
        int_part, frac_part = body, ""
    scale = field.pic.scale
    if len(frac_part) > scale:
        raise RewriteValueError(
            f"field {field.name}: {value!r} has {len(frac_part)} fraction "
            f"digit(s) but the PICTURE scale is {scale}; extra digits "
            "(even zeroes) are rejected rather than truncated")
    digits = int_part + frac_part + "0" * (scale - len(frac_part))
    scaled = int(digits) if digits else 0  # regex guarantees at least one digit
    return scaled, negative


def _encode_number(value: str, field: Field) -> bytes:
    assert field.pic is not None
    pic = field.pic
    scaled, negative = _scaled_integer(value, field)
    if negative and not pic.signed:
        raise RewriteValueError(
            f"field {field.name}: unsigned PICTURE cannot accept a negative "
            f"value ({value!r}), including negative zero")
    if negative and scaled == 0:
        negative = False  # negative zero is written as non-negative zero

    capacity = 10 ** pic.display_length
    if scaled >= capacity:
        raise RewriteValueError(
            f"field {field.name}: scaled value {scaled} does not fit "
            f"PIC {pic.display_length} digit(s); capacity is strictly "
            f"|value| < 10^{pic.display_length} ({capacity})")

    digit_text = f"{scaled:0{pic.display_length}d}"
    if field.usage == "COMP-3":
        return _pack_comp3(digit_text, negative, pic.signed, field)
    return _encode_zoned(digit_text, negative, pic.signed, field)


def _encode_zoned(digit_text: str, negative: bool, signed: bool,
                  field: Field) -> bytes:
    out = bytearray(_ZONED_DIGIT | int(d) for d in digit_text[:-1])
    last_digit = int(digit_text[-1])
    if signed:
        zone = (_ZONED_NEGATIVE_ZONE if negative
                else _ZONED_POSITIVE_ZONE)
    else:
        zone = _ZONED_DIGIT
    out.append(zone | last_digit)
    if len(out) != field.byte_length:  # defensive: layout owns the width
        raise RewriteValueError(
            f"field {field.name}: encoded {len(out)} bytes but the layout "
            f"span is {field.byte_length}")
    return bytes(out)


def _pack_comp3(digit_text: str, negative: bool, signed: bool,
                field: Field) -> bytes:
    sign_nibble = (_COMP3_SIGN_UNSIGNED if not signed
                   else _COMP3_SIGN_NEGATIVE if negative
                   else _COMP3_SIGN_POSITIVE)
    nibbles = [int(d) for d in digit_text]
    nibbles.append(sign_nibble)
    expected_nibbles = field.byte_length * 2
    pad = expected_nibbles - len(nibbles)
    if pad < 0:  # defensive: the capacity check above should prevent this
        raise RewriteValueError(
            f"field {field.name}: value needs {len(nibbles)} nibbles but "
            f"the field holds {expected_nibbles}")
    nibbles = [0] * pad + nibbles
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        out.append((nibbles[i] << 4) | nibbles[i + 1])
    if len(out) != field.byte_length:
        raise RewriteValueError(
            f"field {field.name}: packed {len(out)} bytes but the layout "
            f"span is {field.byte_length}")
    return bytes(out)
