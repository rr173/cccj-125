"""Flatten a parsed layout into leaf fields keyed by full group path.

The impact report never matches fields by bare name: the same name may
appear in several groups, and only the complete group path
(``ACCT-RECORD/GRP/CODE``) identifies a field.  This module walks a parsed
:class:`~copybook.model.Layout` once and records every elementary item
with its absolute byte range and interpretation attributes, plus the
structural facts the report must be honest about:

* duplicate field paths (two declarations with the same group path make
  path-based matching untrustworthy);
* the same leaf name declared in different groups;
* REDEFINES view clusters -- alternative interpretations of one byte
  range, which overlay their target instead of occupying new bytes.

All byte ranges are half-open ``[start, end)``, consistent with the
decoder's ``byte_start``/``byte_end`` convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Optional

from ..model import Field, Layout


def pic_text(category: str, display_length: int, signed: bool,
             scale: int) -> str:
    """Rebuild a readable PICTURE string from parsed attributes."""
    if category == "X":
        return f"X({display_length})"
    integers = display_length - scale
    head = "9" if integers == 1 else f"9({integers})"
    if scale:
        head += "V" + "9" * scale
    return ("S" if signed else "") + head


@dataclass(frozen=True)
class LeafField:
    """One elementary item, identified by its full group path."""

    path: tuple                 # ("ACCT-RECORD", "BALANCE")
    level: int
    offset: int                 # absolute byte offset, first OCCURS iteration
    byte_length: int            # bytes of one OCCURS iteration
    occurs: int                 # fixed OCCURS count
    pic_category: str           # "X" or "9"
    display_length: int
    signed: bool
    scale: int
    usage: str                  # "DISPLAY" or "COMP-3"
    codec: str                  # EBCDIC codec for DISPLAY fields
    filler: bool
    redefines: Optional[tuple]  # group path of the REDEFINES target

    @property
    def name(self) -> str:
        return self.path[-1]

    @property
    def path_text(self) -> str:
        return "/".join(self.path)

    @property
    def total_length(self) -> int:
        return self.byte_length * self.occurs

    @property
    def end(self) -> int:
        """Absolute offset one past the last byte (half-open range)."""
        return self.offset + self.total_length

    @property
    def is_view(self) -> bool:
        return self.redefines is not None

    def pic(self) -> str:
        return pic_text(self.pic_category, self.display_length,
                        self.signed, self.scale)

    def interpretation(self) -> tuple:
        """Attributes deciding how the bytes are read (position excluded).

        The codec only matters for DISPLAY fields: COMP-3 bytes are
        unpacked nibble-wise and never pass through the text codec, so a
        manifest codec switch must not flag packed fields as changed.
        """
        return (self.pic_category, self.display_length, self.signed,
                self.scale, self.usage, self.occurs,
                self.codec if self.usage == "DISPLAY" else None,
                self.redefines)

    def describe(self) -> dict:
        """Machine-readable form used in the JSON field diff."""
        return {
            "path": self.path_text,
            "bytes": [self.offset, self.end],
            "byte_length_per_occurs": self.byte_length,
            "occurs": self.occurs,
            "pic": self.pic(),
            "pic_category": self.pic_category,
            "signed": self.signed,
            "scale": self.scale,
            "usage": self.usage,
            "codec": self.codec if self.usage == "DISPLAY" else None,
            "redefines": "/".join(self.redefines) if self.redefines else None,
        }


@dataclass(frozen=True)
class ViewCluster:
    """A REDEFINES target and every view that reinterprets its bytes.

    The views are *alternative* readings of the target's byte range; they
    do not occupy additional storage and must never be counted as
    simultaneous owners of the range.
    """

    target_path: tuple
    byte_start: int
    byte_end: int
    views: tuple                # view leaf paths, document order


@dataclass(frozen=True)
class DuplicatePath:
    """One group path declared more than once in the same layout."""

    path: tuple
    declarations: tuple         # of (level, offset, kind) per declaration


@dataclass
class FlatLayout:
    """A layout reduced to comparable facts."""

    layout_name: str
    version: str
    codec: str
    record_length: int
    leaves: list[LeafField] = dc_field(default_factory=list)
    duplicate_paths: list[DuplicatePath] = dc_field(default_factory=list)
    shared_names: dict = dc_field(default_factory=dict)   # name -> [paths]
    view_clusters: list[ViewCluster] = dc_field(default_factory=list)
    filler_intervals: list = dc_field(default_factory=list)  # (start, end)

    @property
    def named_leaves(self) -> list[LeafField]:
        """Non-FILLER leaves, the only ones matched across layouts."""
        return [leaf for leaf in self.leaves if not leaf.filler]

    def leaf_map(self) -> dict:
        """Path -> leaf for non-FILLER leaves (empty when ambiguous)."""
        if self.duplicate_paths:
            return {}
        return {leaf.path: leaf for leaf in self.named_leaves}


def _path_of(field: Field) -> tuple:
    parts = []
    node: Optional[Field] = field
    while node is not None:
        parts.append(node.name)
        node = node.parent
    return tuple(reversed(parts))


def flatten_layout(layout: Layout) -> FlatLayout:
    """Walk *layout* once and collect every comparable fact."""
    flat = FlatLayout(layout_name=layout.name, version=layout.version,
                      codec=layout.codec,
                      record_length=layout.record_length)
    declarations: dict = {}
    names: dict = {}
    views_by_target: dict = {}
    target_ranges: dict = {}

    for node in layout.root.iter_fields():
        path = _path_of(node)
        kind = "group" if node.pic is None else "elementary"
        declarations.setdefault(path, []).append(
            (node.level, node.offset, kind))
        if node.pic is None:
            continue

        redefines = (_path_of(node.redefines_target)
                     if node.redefines_target is not None else None)
        leaf = LeafField(
            path=path, level=node.level, offset=node.offset,
            byte_length=node.byte_length, occurs=node.occurs,
            pic_category=node.pic.category,
            display_length=node.pic.display_length,
            signed=node.pic.signed, scale=node.pic.scale,
            usage=node.usage, codec=layout.codec, filler=node.filler,
            redefines=redefines)
        flat.leaves.append(leaf)
        if node.filler:
            flat.filler_intervals.append((leaf.offset, leaf.end))
            continue
        names.setdefault(leaf.name, [])
        if path not in names[leaf.name]:
            names[leaf.name].append(path)
        if redefines is not None:
            views_by_target.setdefault(redefines, []).append(path)
            target = node.redefines_target
            assert target is not None
            target_ranges[redefines] = (
                target.offset, target.offset + target.total_length)

    flat.duplicate_paths = [
        DuplicatePath(path, tuple(decls))
        for path, decls in declarations.items() if len(decls) > 1]
    flat.shared_names = {name: paths
                         for name, paths in names.items() if len(paths) > 1}
    flat.view_clusters = [
        ViewCluster(target, target_ranges[target][0],
                    target_ranges[target][1], tuple(views))
        for target, views in views_by_target.items()]
    return flat
