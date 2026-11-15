"""Byte-building helpers for EBCDIC test fixtures.

These mirror the mainframe encodings so tests can construct realistic
data files without hardcoding hex everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union


def ebcdic(text: str, codec: str = "cp037") -> bytes:
    """Encode text the way the sending mainframe would."""
    return text.encode(codec)


def zoned(value: int, length: int, signed: bool = True) -> bytes:
    """Encode a DISPLAY zoned-decimal field (trailing sign overpunch)."""
    digits = f"{abs(value):0{length}d}"
    if signed:
        table = "{ABCDEFGHI" if value >= 0 else "}JKLMNOPQR"
        digits = digits[:-1] + table[int(digits[-1])]
    return digits.encode("cp037")


def comp3(value: int, num_digits: int, unsigned: bool = False) -> bytes:
    """Encode a COMP-3 packed-decimal field."""
    if unsigned:
        sign_nibble = 0xF
    else:
        sign_nibble = 0xD if value < 0 else 0xC
    digits = f"{abs(value):0{num_digits}d}"
    if len(digits) % 2 == 0:
        digits = "0" + digits
    out = bytearray()
    for i in range(0, len(digits) - 1, 2):
        out.append((int(digits[i]) << 4) | int(digits[i + 1]))
    out.append((int(digits[-1]) << 4) | sign_nibble)
    return bytes(out)


def write_lines(path: Union[str, Path], lines: list[bytes],
                final_newline: bool = True) -> Path:
    """Write raw lines (already EBCDIC bytes) to a data file."""
    path = Path(path)
    data = b"\n".join(lines)
    if final_newline and lines:
        data += b"\n"
    path.write_bytes(data)
    return path
