"""Field-level comparison of two flattened layouts.

Fields are matched by **full group path**, never by bare name.  A matched
pair is classified as:

* ``unchanged``     -- same bytes, same interpretation (原位置同义);
* ``moved``         -- same interpretation, different offset (位置移动);
  the field's own PIC/scale/usage are untouched, it only shifted because
  preceding bytes changed -- reported separately from real redefinitions;
* ``reinterpreted`` -- same path but PIC category/length, sign, scale,
  usage, OCCURS count, codec or REDEFINES target changed (同名但解释变化);
* ``added`` / ``removed`` -- path exists on one side only.

REDEFINES views are compared as *views over a byte range*: a new view
over unchanged bytes is an additional interpretation, not a new
occupant, and never shifts other fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Optional

from .flatten import FlatLayout, LeafField

# field change categories
UNCHANGED = "unchanged"
MOVED = "moved"
REINTERPRETED = "reinterpreted"
ADDED = "added"
REMOVED = "removed"

# comparison conclusions
COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"
UNVERIFIABLE = "unverifiable"


@dataclass
class FieldChange:
    path: tuple
    category: str
    old: Optional[LeafField]
    new: Optional[LeafField]
    changes: list = dc_field(default_factory=list)   # attribute deltas
    note: Optional[str] = None

    @property
    def path_text(self) -> str:
        return "/".join(self.path)

    def describe(self) -> dict:
        out = {"path": self.path_text, "category": self.category}
        if self.old is not None:
            out["old"] = self.old.describe()
        if self.new is not None:
            out["new"] = self.new.describe()
        if self.changes:
            out["changes"] = list(self.changes)
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class ViewClusterChange:
    """How the alternative readings of one byte range evolved."""

    target_path: tuple
    byte_start: int
    byte_end: int
    old_views: tuple
    new_views: tuple

    def describe(self) -> dict:
        return {
            "target": "/".join(self.target_path),
            "bytes": [self.byte_start, self.byte_end],
            "old_views": ["/".join(v) for v in self.old_views],
            "new_views": ["/".join(v) for v in self.new_views],
        }


@dataclass
class LayoutComparison:
    """The field-by-field result for one (old version, new version) pair."""

    old_label: str
    new_label: str
    record_length_old: int
    record_length_new: int
    fields: list = dc_field(default_factory=list)         # FieldChange
    view_changes: list = dc_field(default_factory=list)   # ViewClusterChange
    length_explanation: list = dc_field(default_factory=list)  # dicts
    notes: list = dc_field(default_factory=list)          # strings
    problems: list = dc_field(default_factory=list)       # dicts
    conclusion: str = UNVERIFIABLE

    def changed_fields(self) -> list:
        return [f for f in self.fields if f.category != UNCHANGED]

    def describe(self) -> dict:
        return {
            "old_layout": self.old_label,
            "new_layout": self.new_label,
            "conclusion": self.conclusion,
            "record_length": {
                "old": self.record_length_old,
                "new": self.record_length_new,
                "changed": self.record_length_old != self.record_length_new,
                "explanation": list(self.length_explanation),
            },
            "fields": [f.describe() for f in self.fields],
            "view_clusters": [v.describe() for v in self.view_changes],
            "notes": list(self.notes),
            "problems": list(self.problems),
        }


def _covered_by(intervals, start: int, end: int) -> bool:
    """True when [start, end) is fully covered by the given intervals."""
    for lo, hi in sorted(intervals):
        if lo <= start < hi:
            start = hi
        if start >= end:
            return True
    return start >= end


def _attribute_changes(old: LeafField, new: LeafField) -> list:
    """Human-readable list of interpretation/position deltas."""
    changes = []
    if old.offset != new.offset:
        changes.append(f"offset: {old.offset} -> {new.offset} "
                       f"(shift {new.offset - old.offset:+d})")
    if old.pic_category != new.pic_category or \
            old.display_length != new.display_length or \
            old.signed != new.signed or old.scale != new.scale:
        changes.append(f"pic: {old.pic()} -> {new.pic()}")
    if old.signed != new.signed:
        changes.append(f"signed: {old.signed} -> {new.signed}")
    if old.scale != new.scale:
        changes.append(f"scale: {old.scale} -> {new.scale}")
    if old.usage != new.usage:
        changes.append(f"usage: {old.usage} -> {new.usage}")
    if old.occurs != new.occurs:
        changes.append(f"occurs: {old.occurs} -> {new.occurs}")
    if old.byte_length != new.byte_length:
        changes.append(f"bytes per occurs: {old.byte_length} -> "
                       f"{new.byte_length}")
    if old.usage == "DISPLAY" and new.usage == "DISPLAY" \
            and old.codec != new.codec:
        changes.append(f"codec: {old.codec} -> {new.codec}")
    if old.redefines != new.redefines:
        fmt = lambda p: "/".join(p) if p else "(none)"  # noqa: E731
        changes.append(f"redefines: {fmt(old.redefines)} -> "
                       f"{fmt(new.redefines)}")
    return changes


def compare_layouts(old: FlatLayout, new: FlatLayout,
                    old_label: str, new_label: str) -> LayoutComparison:
    """Compare two flattened layouts field by field."""
    cmp = LayoutComparison(old_label=old_label, new_label=new_label,
                           record_length_old=old.record_length,
                           record_length_new=new.record_length)

    for side, flat in (("old", old), ("new", new)):
        for dup in flat.duplicate_paths:
            decls = ", ".join(
                f"level {lvl} at byte {off} ({kind})"
                for lvl, off, kind in dup.declarations)
            cmp.problems.append({
                "kind": "duplicate_field_path",
                "side": side,
                "layout": flat.layout_name,
                "version": flat.version,
                "path": "/".join(dup.path),
                "message": f"field path {'/'.join(dup.path)} is declared "
                           f"{len(dup.declarations)} times in {side} layout "
                           f"{flat.layout_name} v{flat.version} ({decls}); "
                           "path-based matching is ambiguous",
            })
    if cmp.problems:
        cmp.conclusion = UNVERIFIABLE
        cmp.notes.append("duplicate field paths make the layout "
                         "untrustworthy; field comparison skipped")
        return cmp

    old_map, new_map = old.leaf_map(), new.leaf_map()

    # ---- match by full group path -------------------------------------
    for path, old_leaf in sorted(old_map.items(),
                                 key=lambda kv: (kv[1].offset, kv[0])):
        new_leaf = new_map.get(path)
        if new_leaf is None:
            note = None
            if _covered_by(new.filler_intervals, old_leaf.offset,
                           old_leaf.end):
                note = ("bytes remain in the new layout as FILLER; the "
                        "named field disappears from decoded output")
            cmp.fields.append(FieldChange(path, REMOVED, old_leaf, None,
                                          note=note))
            continue
        if old_leaf.interpretation() == new_leaf.interpretation():
            if old_leaf.offset == new_leaf.offset:
                cmp.fields.append(FieldChange(path, UNCHANGED,
                                              old_leaf, new_leaf))
            else:
                shift = new_leaf.offset - old_leaf.offset
                cmp.fields.append(FieldChange(
                    path, MOVED, old_leaf, new_leaf,
                    changes=[f"offset: {old_leaf.offset} -> "
                             f"{new_leaf.offset} (shift {shift:+d})"],
                    note="field definition unchanged; position shifted by "
                         "length changes elsewhere in the record"))
        else:
            cmp.fields.append(FieldChange(
                path, REINTERPRETED, old_leaf, new_leaf,
                changes=_attribute_changes(old_leaf, new_leaf)))

    for path, new_leaf in sorted(new_map.items(),
                                 key=lambda kv: (kv[1].offset, kv[0])):
        if path in old_map:
            continue
        if new_leaf.is_view:
            note = (f"new REDEFINES view over bytes "
                    f"[{new_leaf.offset}, {new_leaf.end}) of "
                    f"{'/'.join(new_leaf.redefines)}; an alternative "
                    "reading of existing bytes, no additional storage")
        elif _covered_by(old.filler_intervals, new_leaf.offset,
                         new_leaf.end):
            note = (f"occupies bytes [{new_leaf.offset}, {new_leaf.end}) "
                    "that were FILLER in the old layout")
        else:
            note = None
        cmp.fields.append(FieldChange(path, ADDED, None, new_leaf,
                                      note=note))

    # ---- same name in different groups ---------------------------------
    for side, flat in (("old", old), ("new", new)):
        for name, paths in sorted(flat.shared_names.items()):
            listing = ", ".join("/".join(p) for p in paths)
            cmp.notes.append(
                f"name {name} appears in multiple groups of the {side} "
                f"layout ({listing}); matched by full group path, not by "
                "name")
    removed_names = {f.old.name for f in cmp.fields
                     if f.category == REMOVED}
    added_names = {f.new.name for f in cmp.fields if f.category == ADDED}
    for name in sorted(removed_names & added_names):
        old_paths = [f.path_text for f in cmp.fields
                     if f.category == REMOVED and f.old.name == name]
        new_paths = [f.path_text for f in cmp.fields
                     if f.category == ADDED and f.new.name == name]
        cmp.notes.append(
            f"name {name} exists on both sides but at different group "
            f"paths ({', '.join(old_paths)} vs {', '.join(new_paths)}); "
            "treated as removed + added, not as the same field")

    # ---- REDEFINES view clusters ---------------------------------------
    old_clusters = {c.target_path: c for c in old.view_clusters}
    new_clusters = {c.target_path: c for c in new.view_clusters}
    for target in sorted(set(old_clusters) | set(new_clusters)):
        oc = old_clusters.get(target)
        nc = new_clusters.get(target)
        old_views = oc.views if oc else ()
        new_views = nc.views if nc else ()
        if old_views == new_views:
            continue
        ref = nc or oc
        cmp.view_changes.append(ViewClusterChange(
            target, ref.byte_start, ref.byte_end, old_views, new_views))
        gained = [v for v in new_views if v not in old_views]
        lost = [v for v in old_views if v not in new_views]
        where = f"[{ref.byte_start}, {ref.byte_end})"
        for view in gained:
            cmp.notes.append(
                f"bytes {where} of {'/'.join(target)} gain an alternative "
                f"reading via new REDEFINES view {'/'.join(view)}; the "
                "view overlays the same bytes and occupies no extra "
                "storage")
        for view in lost:
            cmp.notes.append(
                f"REDEFINES view {'/'.join(view)} of bytes {where} no "
                "longer exists in the new layout")

    # ---- record length explanation --------------------------------------
    if old.record_length != new.record_length:
        delta = new.record_length - old.record_length
        cmp.notes.append(
            f"record length changes {old.record_length} -> "
            f"{new.record_length} bytes ({delta:+d})")
    for f in cmp.fields:
        if f.category == ADDED and not f.new.is_view:
            cmp.length_explanation.append({
                "path": f.path_text, "effect": "added",
                "bytes": f.new.total_length})
        elif f.category == REMOVED:
            cmp.length_explanation.append({
                "path": f.path_text, "effect": "removed",
                "bytes": -f.old.total_length})
        elif f.category == REINTERPRETED and \
                f.old.total_length != f.new.total_length:
            cmp.length_explanation.append({
                "path": f.path_text, "effect": "resized",
                "bytes": f.new.total_length - f.old.total_length})
    old_fill = sum(hi - lo for lo, hi in old.filler_intervals)
    new_fill = sum(hi - lo for lo, hi in new.filler_intervals)
    if old_fill != new_fill:
        cmp.length_explanation.append({
            "path": "(FILLER)", "effect": "filler resized",
            "bytes": new_fill - old_fill})
    accounted = sum(item["bytes"] for item in cmp.length_explanation)
    if accounted != new.record_length - old.record_length:
        cmp.notes.append(
            f"warning: length contributors add up to {accounted:+d} but "
            f"the record length delta is "
            f"{new.record_length - old.record_length:+d}")

    # ---- conclusion ------------------------------------------------------
    breaking = [f for f in cmp.fields
                if f.category in (REMOVED, MOVED, REINTERPRETED)]
    unplaced = [f for f in cmp.fields
                if f.category == ADDED and not f.new.is_view
                and not _covered_by(old.filler_intervals, f.new.offset,
                                    f.new.end)]
    if breaking or unplaced or old.record_length != new.record_length:
        cmp.conclusion = INCOMPATIBLE
    else:
        cmp.conclusion = COMPATIBLE
    return cmp
