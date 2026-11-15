"""Data model for parsed copybook layouts.

All lengths and offsets are in **bytes**, not characters.  For DISPLAY
fields one character occupies one byte in the single-byte EBCDIC codecs we
support, but COMP-3 fields pack two digits per byte, so byte width and
display width genuinely differ.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field as dc_field
from typing import Iterator, Optional


@dataclass
class Pic:
    """A parsed PICTURE clause (subset: X / A / 9 with optional S and V)."""

    category: str          # "X" (alphanumeric) or "9" (numeric)
    display_length: int    # number of character positions (digits for "9")
    signed: bool = False   # S present in the picture
    scale: int = 0         # digits after the implied decimal point V


@dataclass
class Field:
    """One node of the layout tree (group or elementary item)."""

    name: str
    level: int
    pic: Optional[Pic] = None
    usage: str = "DISPLAY"            # "DISPLAY" or "COMP-3"
    occurs: int = 1                   # fixed OCCURS count
    redefines: Optional[str] = None   # name of the REDEFINES target, if any
    filler: bool = False
    children: list["Field"] = dc_field(default_factory=list)
    parent: Optional["Field"] = dc_field(default=None, repr=False)
    # Filled in by the layout pass:
    offset: int = 0                   # absolute byte offset (iteration 0)
    byte_length: int = 0              # bytes of one OCCURS iteration
    redefines_target: Optional["Field"] = dc_field(default=None, repr=False)

    @property
    def is_group(self) -> bool:
        return self.pic is None

    @property
    def total_length(self) -> int:
        """Bytes occupied including all OCCURS iterations."""
        return self.byte_length * self.occurs

    def iter_fields(self) -> Iterator["Field"]:
        """Yield this field and all descendants, document order."""
        yield self
        for child in self.children:
            yield from child.iter_fields()


@dataclass
class Layout:
    """A fully parsed and offset-resolved copybook."""

    name: str
    version: str
    root: Field
    record_length: int                # record size in bytes
    codec: str = "cp037"              # EBCDIC codec used for DISPLAY fields

    def find(self, name: str) -> Optional[Field]:
        """Find a field by (case-insensitive) name, or None."""
        target = name.upper()
        queue: deque[Field] = deque([self.root])
        while queue:
            node = queue.popleft()
            if node.name == target:
                return node
            queue.extend(node.children)
        return None
