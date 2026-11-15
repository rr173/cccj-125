"""Record decoder: turns fixed-width EBCDIC byte strings into values.

Decoding is strictly byte-oriented: the record is sliced at byte offsets
*before* any character decoding happens, so packed (COMP-3) fields and
multi-byte text encodings can never shift field boundaries.

Sign handling
-------------
* Zoned (DISPLAY) numerics carry the sign as an overpunch in the **last**
  byte.  After EBCDIC decoding this shows up as ``{A-I`` (positive) or
  ``}J-R`` (negative); plain digits mean unsigned/positive.
* COMP-3 numerics carry the sign in the last nibble: C/A/E/F positive,
  B/D negative.
* ``V`` in the PICTURE is an implied decimal point; values are returned as
  :class:`decimal.Decimal` to avoid binary floating point error.

REDEFINES views
---------------
A REDEFINES field is an *alternative interpretation* of bytes owned by its
target.  Views are best-effort: if a view does not fit the bytes (e.g. a
numeric view over text) the view decodes to ``None`` and a
:class:`FieldDecodeError` is recorded in the returned error list -- the
record itself is **not** rejected.  Which views are decoded is controlled
by :class:`RedefinesPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Optional

from .errors import CopybookError, FieldDecodeError, RecordLengthError
from .model import Field, Layout

_ZONED_POSITIVE = "{ABCDEFGHI"
_ZONED_NEGATIVE = "}JKLMNOPQR"
_POSITIVE_NIBBLES = (0xC, 0xA, 0xE, 0xF)
_NEGATIVE_NIBBLES = (0xD, 0xB)


@dataclass
class RedefinesPolicy:
    """Selects which REDEFINES views are decoded into the output.

    * ``include``: explicit allow-list of view names; all other views are
      omitted.  ``None`` (default) decodes every view.
    * ``discriminator`` + ``cases`` (+ optional ``default``): choose exactly
      one view per record from the value of another field, e.g. a record
      type code.  ``cases`` maps the discriminator's string value to a view
      name; ``default`` is used when no case matches (``None`` = no view).

    The two modes are mutually exclusive.
    """

    include: Optional[tuple[str, ...]] = None
    discriminator: Optional[str] = None
    cases: dict[str, str] = dc_field(default_factory=dict)
    default: Optional[str] = None

    def __post_init__(self) -> None:
        if self.include is not None and self.discriminator is not None:
            raise ValueError("RedefinesPolicy: use either 'include' or "
                             "'discriminator', not both")
        if self.include is not None:
            self.include = tuple(n.upper() for n in self.include)
        if self.discriminator is not None:
            self.discriminator = self.discriminator.upper()
        self.cases = {str(k): str(v).upper() for k, v in self.cases.items()}
        if self.default is not None:
            self.default = self.default.upper()


class Decoder:
    """Decodes records of one :class:`Layout`."""

    def __init__(self, layout: Layout, policy: Optional[RedefinesPolicy] = None):
        self.layout = layout
        self.policy = policy or RedefinesPolicy()
        self._disc_field: Optional[Field] = None
        if self.policy.discriminator is not None:
            f = layout.find(self.policy.discriminator)
            if f is None:
                raise CopybookError(
                    f"discriminator field {self.policy.discriminator} not "
                    f"found in layout {layout.name}")
            if f.pic is None:
                raise CopybookError(
                    f"discriminator field {f.name} must be elementary")
            node: Optional[Field] = f
            while node is not None:
                if node.occurs > 1:
                    raise CopybookError(
                        f"discriminator field {f.name} must not be inside "
                        "an OCCURS")
                node = node.parent
            self._disc_field = f

    # ------------------------------------------------------------ public

    def decode(self, data: bytes) -> tuple[dict, list[FieldDecodeError]]:
        """Decode one record.

        Returns ``(fields, view_errors)``.  Raises
        :class:`RecordLengthError` or :class:`FieldDecodeError` for
        malformed records (the caller quarantines the line).
        """
        if len(data) != self.layout.record_length:
            raise RecordLengthError(self.layout.record_length, len(data))
        selected = self._select_view(data)
        errors: list[FieldDecodeError] = []
        root = self.layout.root
        if root.pic is not None:  # degenerate single-field record
            return {root.name: self._decode_elementary(root, 0, data)}, errors
        out: dict = {}
        self._decode_children(root, 0, data, out, errors, selected)
        return out, errors

    # ------------------------------------------------------------- walk

    def _decode_children(self, group: Field, shift: int, data: bytes,
                         out: dict, errors: list[FieldDecodeError],
                         selected: Optional[str]) -> None:
        for child in group.children:
            if child.filler:
                continue
            if child.redefines_target is not None:
                if not self._view_selected(child.name, selected):
                    continue
                try:
                    out[child.name] = self._decode_node(child, shift, data,
                                                        errors, selected)
                except FieldDecodeError as exc:
                    out[child.name] = None
                    errors.append(exc)
            else:
                out[child.name] = self._decode_node(child, shift, data,
                                                    errors, selected)

    def _decode_node(self, f: Field, shift: int, data: bytes,
                     errors: list[FieldDecodeError],
                     selected: Optional[str]):
        if f.pic is not None:
            if f.occurs > 1:
                return [self._decode_elementary(f, shift + i * f.byte_length,
                                                data)
                        for i in range(f.occurs)]
            return self._decode_elementary(f, shift, data)
        if f.occurs > 1:
            return [self._decode_group_value(f, shift + i * f.byte_length,
                                             data, errors, selected)
                    for i in range(f.occurs)]
        return self._decode_group_value(f, shift, data, errors, selected)

    def _decode_group_value(self, f: Field, shift: int, data: bytes,
                            errors: list[FieldDecodeError],
                            selected: Optional[str]) -> dict:
        out: dict = {}
        self._decode_children(f, shift, data, out, errors, selected)
        return out

    # ------------------------------------------------------- elementary

    def _decode_elementary(self, f: Field, shift: int, data: bytes):
        start = f.offset + shift
        raw = data[start:start + f.byte_length]
        assert f.pic is not None
        if f.usage == "COMP-3":
            return _decode_comp3(raw, f, start)
        try:
            text = raw.decode(self.layout.codec)
        except UnicodeDecodeError as exc:
            raise FieldDecodeError(
                f.name, start,
                f"cannot decode as {self.layout.codec}: {exc}")
        if f.pic.category == "X":
            return text.rstrip(" ")
        return _decode_zoned(text, f, start)

    # ------------------------------------------------- redefines policy

    def _select_view(self, data: bytes) -> Optional[str]:
        """Resolve the discriminator to the selected view name (or None)."""
        if self.policy.discriminator is None:
            return None
        assert self._disc_field is not None
        try:
            raw = self._decode_elementary(self._disc_field, 0, data)
            key = str(raw)
        except FieldDecodeError:
            key = ""
        return self.policy.cases.get(key, self.policy.default)

    def _view_selected(self, name: str, selected: Optional[str]) -> bool:
        if self.policy.discriminator is not None:
            return name == selected
        if self.policy.include is not None:
            return name in self.policy.include
        return True


# ------------------------------------------------------------- primitives

def _decode_zoned(text: str, f: Field, offset: int):
    """Decode a DISPLAY numeric with trailing sign overpunch."""
    assert f.pic is not None
    last, body = text[-1], text[:-1]
    if "0" <= last <= "9":
        digit, sign = last, 1
    elif last in _ZONED_POSITIVE:
        digit, sign = str(_ZONED_POSITIVE.index(last)), 1
    elif last in _ZONED_NEGATIVE:
        digit, sign = str(_ZONED_NEGATIVE.index(last)), -1
    else:
        raise FieldDecodeError(
            f.name, offset, f"invalid sign overpunch character {last!r}")
    if not f.pic.signed:
        sign = 1  # unsigned pictures tolerate and ignore an overpunch
    digits = body + digit
    if not digits or any(c < "0" or c > "9" for c in digits):
        raise FieldDecodeError(
            f.name, offset, f"non-numeric zoned content {text!r}")
    return _apply_scale(int(digits) * sign, f.pic.scale)


def _decode_comp3(raw: bytes, f: Field, offset: int):
    """Decode a COMP-3 packed decimal."""
    assert f.pic is not None
    digits: list[int] = []
    sign_nibble = 0xF
    for idx, byte in enumerate(raw):
        hi, lo = byte >> 4, byte & 0xF
        if hi > 9:
            raise FieldDecodeError(
                f.name, offset + idx,
                f"invalid packed digit nibble 0x{hi:X}")
        digits.append(hi)
        if idx == len(raw) - 1:
            sign_nibble = lo
        else:
            if lo > 9:
                raise FieldDecodeError(
                    f.name, offset + idx,
                    f"invalid packed digit nibble 0x{lo:X}")
            digits.append(lo)
    if sign_nibble in _POSITIVE_NIBBLES:
        sign = 1
    elif sign_nibble in _NEGATIVE_NIBBLES:
        sign = -1
    else:
        raise FieldDecodeError(
            f.name, offset + len(raw) - 1,
            f"invalid sign nibble 0x{sign_nibble:X}")
    if not f.pic.signed:
        sign = 1
    extra = len(digits) - f.pic.display_length
    if extra < 0 or any(d != 0 for d in digits[:extra]):
        raise FieldDecodeError(
            f.name, offset,
            f"packed digit count does not match PICTURE "
            f"(expected {f.pic.display_length} digits)")
    value = 0
    for d in digits[extra:]:
        value = value * 10 + d
    return _apply_scale(value * sign, f.pic.scale)


def _apply_scale(value: int, scale: int):
    if scale:
        return Decimal(value).scaleb(-scale)
    return value
