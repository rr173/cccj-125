"""Resolve a correction path to a unique elementary, non-FILLER field.

A path starts at the children of the record root group (the root name is
not part of the path):

* a string component is the original layout field name, compared in
  upper case;
* an integer component is a zero-based index into a fixed
  ``OCCURS > 1`` layer.  Every nested OCCURS layer needs its own index;
  an array layer may never be named without an index;
* the final target must be an elementary item that is not FILLER.
  Groups, OCCURS arrays as a whole and anonymous fillers are not
  writable;
* a REDEFINES base field and any of its alternative views can each be
  named explicitly -- the active view is never guessed from a
  discriminator or from the content.

Ambiguity (a name matching several siblings, an index out of range, a
non-integer where an index is required, ...) is reported rather than
resolved heuristically.
"""

from __future__ import annotations

from ..model import Field, Layout
from .errors import RewriteOrderError


class ResolvedField:
    """A located elementary field with its record-relative byte span."""

    __slots__ = ("field", "start", "end", "path")

    def __init__(self, field: Field, start: int, end: int, path: list):
        self.field = field
        self.start = start          # absolute offset within the record
        self.end = end
        self.path = path            # normalized path (strings + ints)


def _is_plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_path(layout: Layout, raw_path) -> ResolvedField:
    if not isinstance(raw_path, list) or not raw_path:
        raise RewriteOrderError(
            "edit path must be a non-empty array starting at a child of "
            "the record root group")

    node: Field = layout.root
    shift = 0                     # offset added by OCCURS indices so far
    normalized: list = []
    i = 0

    while i < len(raw_path):
        component = raw_path[i]
        where = f"path {_render(raw_path)} position {i}"
        if not isinstance(component, str):
            raise RewriteOrderError(
                f"{where}: expected an upper-case field name (string), got "
                f"{component!r}")
        name = component.upper()
        matches = [c for c in node.children if c.name == name]
        if not matches:
            raise RewriteOrderError(
                f"{where}: field {component!r} does not exist under "
                f"{_node_label(node, layout)}")
        if len(matches) > 1:
            raise RewriteOrderError(
                f"{where}: field name {component!r} is not unique under "
                f"{_node_label(node, layout)}; the path does not locate a "
                "single field")
        child = matches[0]
        normalized.append(child.name)

        if child.occurs > 1:
            # An OCCURS layer must be followed immediately by an index.
            if i + 1 >= len(raw_path):
                raise RewriteOrderError(
                    f"{where}: {child.name} is an OCCURS({child.occurs}) "
                    "array; provide a zero-based index before descending "
                    "into it (the array as a whole is not writable)")
            index = raw_path[i + 1]
            if not _is_plain_int(index):
                raise RewriteOrderError(
                    f"{where}: {child.name} OCCURS({child.occurs}) needs a "
                    f"zero-based integer index, got {index!r}")
            if index < 0:
                raise RewriteOrderError(
                    f"{where}: OCCURS index must be non-negative, got "
                    f"{index}")
            if index >= child.occurs:
                raise RewriteOrderError(
                    f"{where}: OCCURS index {index} is out of range for "
                    f"{child.name} OCCURS({child.occurs})")
            normalized.append(index)
            shift += index * child.byte_length
            node = child          # continue inside this iteration
            i += 2
            continue

        node = child
        i += 1

    if node.filler:
        raise RewriteOrderError(
            f"path {_render(raw_path)} resolves to FILLER; anonymous "
            "fillers are not writable")
    if node.is_group:
        raise RewriteOrderError(
            f"path {_render(raw_path)} resolves to group {node.name}; only "
            "elementary fields are writable")
    if node.pic is None:
        raise RewriteOrderError(
            f"path {_render(raw_path)} does not resolve to an elementary "
            "field")
    start = node.offset + shift
    end = start + node.byte_length
    return ResolvedField(node, start, end, normalized)


def _node_label(node: Field, layout: Layout) -> str:
    if node is layout.root:
        return f"record {node.name}"
    return f"group {node.name}"


def _render(raw_path) -> str:
    return "/".join(str(c) for c in raw_path)
