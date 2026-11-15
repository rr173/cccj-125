"""Impact report engine: compare two receiving manifests layout by layout.

The engine reads two manifests (old / new) and every copybook they
reference.  It never opens a business data file, never decodes a record
and never runs the batch handoff auditor -- its only question is: *for
each sender and each business date, does the layout selected by the new
manifest still read the same bytes the same way?*

Findings that make a trustworthy comparison impossible -- overlapping
effective ranges, coverage gaps, a missing old baseline, copybook files
that cannot be found or parsed, duplicate field paths -- are collected
as structured *problems* naming the sender, the date range and the
manifest source.  When any problem exists the overall conclusion is
``unverifiable``: the report refuses to look complete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Union

from ..errors import CopybookError
from ..manifest import LayoutRef, Manifest
from ..parser import parse_copybook
from .diff import (COMPATIBLE, INCOMPATIBLE, UNCHANGED, UNVERIFIABLE,
                   LayoutComparison, compare_layouts)
from .errors import ImpactError
from .flatten import FlatLayout, flatten_layout

REPORT_JSON = "impact_report.json"
REPORT_TXT = "impact_report.txt"

_ONE_DAY = timedelta(days=1)
_OPEN = date.max            # internal stand-in for an open interval end

_STATUS_ORDER = {"unaffected": 0, COMPATIBLE: 1, INCOMPATIBLE: 2,
                 UNVERIFIABLE: 3}


@dataclass
class _Side:
    """One manifest plus everything that could be loaded for it."""

    label: str                  # "old_manifest" | "new_manifest"
    path: Path
    entries: list = dc_field(default_factory=list)          # LayoutRef
    layouts: dict = dc_field(default_factory=dict)          # -> FlatLayout


# ------------------------------------------------------------- manifest load

def _problem(kind: str, source: Optional[str], message: str,
             sender: Optional[str] = None,
             copybook: Optional[str] = None,
             rng: Optional[dict] = None, **extra) -> dict:
    out = {"kind": kind, "source": source, "sender": sender,
           "copybook": copybook, "range": rng, "message": message}
    out.update(extra)
    return out


def _range_text(rng: dict) -> str:
    end = rng["to"] if rng["to"] is not None else "开放"
    return f"[{rng['from']} .. {end}]"


def _iso(day: date) -> str:
    return day.isoformat()


def _load_side(path: Union[str, Path], label: str,
               problems: list) -> _Side:
    """Load one manifest tolerantly: record problems, never raise.

    Entry-level validation reuses :meth:`Manifest._parse_entry` so the
    acceptance rules stay identical to the decoder's; the difference is
    that failures become report problems instead of aborting the run.
    """
    side = _Side(label=label, path=Path(path))
    try:
        data = json.loads(side.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(_problem(
            "manifest_unreadable", label,
            f"cannot read manifest {side.path}: {exc}"))
        return side
    if not isinstance(data, dict) or not isinstance(data.get("layouts"),
                                                    list):
        problems.append(_problem(
            "manifest_invalid", label,
            f"manifest {side.path}: expected an object with a 'layouts' "
            "list"))
        return side
    base_dir = side.path.resolve().parent

    for index, raw in enumerate(data["layouts"]):
        try:
            entry = Manifest._parse_entry(index, raw)
        except CopybookError as exc:
            problems.append(_problem(
                "manifest_entry_invalid", label,
                f"layouts[{index}]: {exc}"))
            continue
        side.entries.append(entry)

    # overlapping effective ranges for the same (sender, copybook)
    by_key: dict = {}
    for entry in side.entries:
        by_key.setdefault((entry.sender, entry.copybook), []).append(entry)
    for (sender, copybook), entries in sorted(by_key.items()):
        entries = sorted(entries, key=lambda e: e.effective_from)
        for i, first in enumerate(entries):
            first_end = first.effective_to or _OPEN
            for second in entries[i + 1:]:
                second_end = second.effective_to or _OPEN
                lo = max(first.effective_from, second.effective_from)
                hi = min(first_end, second_end)
                if lo <= hi:
                    rng = {"from": _iso(lo),
                           "to": None if hi == _OPEN else _iso(hi)}
                    problems.append(_problem(
                        "overlap", label,
                        f"effective ranges overlap on "
                        f"{_range_text(rng)}: version {first.version} "
                        f"({first.effective_from}.."
                        f"{first.effective_to or 'open'}) vs version "
                        f"{second.version} ({second.effective_from}.."
                        f"{second.effective_to or 'open'}); no single "
                        "layout can be picked for these dates",
                        sender=sender, copybook=copybook, rng=rng,
                        versions=sorted({first.version,
                                         second.version})))

    # referenced copybooks
    seen: dict = {}
    for entry in side.entries:
        key = (entry.copybook, entry.version)
        rng = {"from": _iso(entry.effective_from),
               "to": None if entry.effective_to is None
               else _iso(entry.effective_to)}
        signature = (entry.file, entry.codec)
        if key in seen:
            if seen[key][0] != signature:
                problems.append(_problem(
                    "copybook_conflict", label,
                    f"version {entry.version} of {entry.copybook} is "
                    f"defined by both {seen[key][0][0]!r} (codec "
                    f"{seen[key][0][1]}) and {entry.file!r} (codec "
                    f"{entry.codec}); the same version must mean one "
                    "layout",
                    sender=entry.sender, copybook=entry.copybook, rng=rng,
                    version=entry.version))
            continue
        cpy_path = base_dir / entry.file
        if not cpy_path.is_file():
            problems.append(_problem(
                "copybook_not_found", label,
                f"copybook file {cpy_path} referenced by "
                f"sender={entry.sender} {entry.copybook} "
                f"v{entry.version} does not exist",
                sender=entry.sender, copybook=entry.copybook, rng=rng,
                version=entry.version, file=str(cpy_path)))
            seen[key] = (signature, None)
            continue
        try:
            layout = parse_copybook(cpy_path.read_text(encoding="utf-8"),
                                    name=entry.copybook,
                                    version=entry.version,
                                    codec=entry.codec)
        except CopybookError as exc:
            problems.append(_problem(
                "copybook_parse_error", label,
                f"copybook {cpy_path}: {exc}",
                sender=entry.sender, copybook=entry.copybook, rng=rng,
                version=entry.version, file=str(cpy_path)))
            seen[key] = (signature, None)
            continue
        seen[key] = (signature, flatten_layout(layout))
    side.layouts = {key: flat for key, (_, flat) in seen.items()
                    if flat is not None}
    return side


# ---------------------------------------------------------- date arithmetic

def _resolve(entries: list, day: date) -> tuple:
    """("none",) | ("ok", version) | ("conflict", versions) for *day*."""
    hits = [e for e in entries
            if e.effective_from <= day <= (e.effective_to or _OPEN)]
    if not hits:
        return ("none", None)
    versions = sorted({e.version for e in hits})
    if len(versions) > 1:
        return ("conflict", tuple(versions))
    return ("ok", versions[0])


def _coverage_union(entries: list) -> list:
    """Merged [from, to] intervals covered by at least one entry."""
    merged: list = []
    for lo, hi in sorted((e.effective_from, e.effective_to or _OPEN)
                         for e in entries):
        if merged:
            plo, phi = merged[-1]
            if phi == _OPEN or lo <= phi + _ONE_DAY:
                merged[-1] = (plo, max(phi, hi))
                continue
        merged.append((lo, hi))
    return merged


def _key_segments(old_entries: list, new_entries: list) -> list:
    """Elementary date ranges with constant resolution on both sides.

    Returns merged segments ``[from, to, old_resolution,
    new_resolution]``; effective ranges are closed intervals and an
    open-ended ``effective_to`` is represented by ``date.max`` here.
    """
    points = set()
    for entry in old_entries + new_entries:
        points.add(entry.effective_from)
        if entry.effective_to is not None and entry.effective_to < _OPEN:
            points.add(entry.effective_to + _ONE_DAY)
    segments = []
    for lo, hi in _coverage_union(old_entries + new_entries):
        cuts = sorted(p for p in points if lo < p <= hi)
        starts = [lo] + cuts
        for i, start in enumerate(starts):
            end = (starts[i + 1] - _ONE_DAY) if i + 1 < len(starts) else hi
            segments.append([start, end, _resolve(old_entries, start),
                             _resolve(new_entries, start)])
    merged: list = []
    for seg in segments:
        if merged and merged[-1][2] == seg[2] and merged[-1][3] == seg[3] \
                and merged[-1][1] != _OPEN \
                and merged[-1][1] + _ONE_DAY == seg[0]:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    return merged


def _layout_identity(flat: FlatLayout) -> tuple:
    """Canonical form used to detect a version reused for a new layout."""
    return (flat.record_length, flat.codec, tuple(flat.leaves))


# --------------------------------------------------------------- key analysis

def _analyse_key(sender: str, copybook: str, old_side: _Side,
                 new_side: _Side, problems: list) -> dict:
    old_entries = [e for e in old_side.entries
                   if (e.sender, e.copybook) == (sender, copybook)]
    new_entries = [e for e in new_side.entries
                   if (e.sender, e.copybook) == (sender, copybook)]
    result = {"sender": sender, "copybook": copybook,
              "affected_ranges": [], "unaffected_ranges": [],
              "unresolvable_ranges": [], "comparisons": []}
    comparisons: list = result["comparisons"]
    comp_index: dict = {}

    for start, end, old_res, new_res in _key_segments(old_entries,
                                                      new_entries):
        rng = {"from": _iso(start),
               "to": None if end == _OPEN else _iso(end)}
        if old_res[0] == "conflict" or new_res[0] == "conflict":
            side = "old_manifest" if old_res[0] == "conflict" \
                else "new_manifest"
            versions = old_res[1] if old_res[0] == "conflict" \
                else new_res[1]
            result["unresolvable_ranges"].append({
                **rng, "reason": "overlap", "source": side,
                "versions": list(versions)})
            continue
        if old_res[0] == "none":
            problems.append(_problem(
                "no_old_baseline", "old_manifest",
                f"new manifest selects {copybook} v{new_res[1]} for "
                f"sender={sender} on {_range_text(rng)}, but the old "
                "manifest has no layout at all for these dates; there is "
                "no baseline to prove compatibility against",
                sender=sender, copybook=copybook, rng=rng,
                new_version=new_res[1]))
            result["unresolvable_ranges"].append({
                **rng, "reason": "no_old_baseline",
                "new_version": new_res[1]})
            continue
        if new_res[0] == "none":
            problems.append(_problem(
                "gap", "new_manifest",
                f"sender={sender} {copybook} v{old_res[1]} served "
                f"{_range_text(rng)} in the old manifest, but the new "
                "manifest covers none of these dates; files arriving for "
                "them would have no layout at all",
                sender=sender, copybook=copybook, rng=rng,
                old_version=old_res[1]))
            result["unresolvable_ranges"].append({
                **rng, "reason": "gap_in_new", "old_version": old_res[1]})
            continue

        old_v, new_v = old_res[1], new_res[1]
        old_flat = old_side.layouts.get((copybook, old_v))
        new_flat = new_side.layouts.get((copybook, new_v))
        if old_flat is None or new_flat is None:
            missing = []
            if old_flat is None:
                missing.append(f"{copybook} v{old_v} (old manifest)")
            if new_flat is None:
                missing.append(f"{copybook} v{new_v} (new manifest)")
            result["unresolvable_ranges"].append({
                **rng, "reason": "copybook_unavailable",
                "old_version": old_v, "new_version": new_v,
                "missing": missing})
            continue

        if old_v == new_v:
            if _layout_identity(old_flat) != _layout_identity(new_flat):
                problems.append(_problem(
                    "version_reused", None,
                    f"sender={sender} {copybook} v{old_v} keeps the same "
                    f"version number on {_range_text(rng)} but the two "
                    "manifests resolve it to different layouts; a "
                    "version must identify exactly one layout",
                    sender=sender, copybook=copybook, rng=rng,
                    version=old_v))
                result["unresolvable_ranges"].append({
                    **rng, "reason": "version_reused", "version": old_v})
            else:
                result["unaffected_ranges"].append(
                    {**rng, "version": old_v})
            continue

        pair = (old_v, new_v)
        if pair not in comp_index:
            comparison = compare_layouts(
                old_flat, new_flat,
                f"{copybook} v{old_v} (old manifest)",
                f"{copybook} v{new_v} (new manifest)")
            for prob in comparison.problems:
                problems.append(_problem(
                    prob["kind"], f"{prob['side']}_manifest",
                    prob["message"], sender=sender, copybook=copybook,
                    rng=None, path=prob["path"],
                    layout=prob["layout"], version=prob["version"]))
            comp_index[pair] = len(comparisons)
            comparisons.append(comparison)
        comparison = comparisons[comp_index[pair]]
        result["affected_ranges"].append({
            **rng, "old_version": old_v, "new_version": new_v,
            "conclusion": comparison.conclusion,
            "comparison": comp_index[pair]})
    return result


def _key_status(key_result: dict) -> str:
    if key_result["unresolvable_ranges"]:
        return UNVERIFIABLE
    conclusions = [c.conclusion for c in key_result["comparisons"]]
    if not conclusions:
        return "unaffected"
    return max(conclusions, key=lambda c: _STATUS_ORDER[c])


# ------------------------------------------------------------------- report

def _leaf_text(leaf) -> str:
    codec = f" codec={leaf.codec}" if leaf.usage == "DISPLAY" else ""
    sign = "signed" if leaf.signed else "unsigned"
    view = (f" REDEFINES {'/'.join(leaf.redefines)}"
            if leaf.is_view else "")
    return (f"[{leaf.offset}, {leaf.end}) {leaf.pic()} {leaf.usage} "
            f"{sign} scale={leaf.scale} occurs={leaf.occurs}"
            f"{codec}{view}")


_CATEGORY_LABEL = {
    "unchanged": "原位置同义 unchanged",
    "moved": "位置移动 moved",
    "reinterpreted": "解释变化 reinterpreted",
    "added": "新增 added",
    "removed": "移除 removed",
}


def _render_comparison(lines: list, cmp: LayoutComparison) -> None:
    lines.append(f"    字段对照: {cmp.old_label}  ->  {cmp.new_label}")
    lines.append(f"    结论: {cmp.conclusion}")
    if cmp.record_length_old != cmp.record_length_new:
        delta = cmp.record_length_new - cmp.record_length_old
        lines.append(f"    记录长度: {cmp.record_length_old} -> "
                     f"{cmp.record_length_new} 字节 ({delta:+d})，构成:")
        for item in cmp.length_explanation:
            lines.append(f"      {item['bytes']:+d}  {item['path']}"
                         f"  ({item['effect']})")
    else:
        lines.append(f"    记录长度: {cmp.record_length_old} 字节 (不变)")
    for f in cmp.fields:
        label = _CATEGORY_LABEL[f.category]
        lines.append(f"      [{label}]  {f.path_text}")
        if f.old is not None and f.category != UNCHANGED:
            lines.append(f"          旧: {_leaf_text(f.old)}")
        if f.new is not None and f.category != UNCHANGED:
            lines.append(f"          新: {_leaf_text(f.new)}")
        if f.category == UNCHANGED:
            lines.append(f"          {_leaf_text(f.old)}")
        for change in f.changes:
            lines.append(f"          {change}")
        if f.note:
            lines.append(f"          说明: {f.note}")
    if cmp.view_changes:
        lines.append("    REDEFINES 视图簇 (同一字节的互斥解释, "
                     "不额外占用存储):")
        for vc in cmp.view_changes:
            old_views = ", ".join("/".join(v) for v in vc.old_views) \
                or "(无)"
            new_views = ", ".join("/".join(v) for v in vc.new_views) \
                or "(无)"
            lines.append(f"      字节 [{vc.byte_start}, {vc.byte_end}) "
                         f"{'/'.join(vc.target_path)}")
            lines.append(f"          旧视图: {old_views}")
            lines.append(f"          新视图: {new_views}")
    for note in cmp.notes:
        lines.append(f"    备注: {note}")


def _render_text(report: dict, key_results: list) -> str:
    lines = [
        "布局变更影响报告 / Layout Change Impact Report",
        "=" * 46,
        f"旧清单 (old manifest): {report['sources']['old_manifest']}",
        f"新清单 (new manifest): {report['sources']['new_manifest']}",
        "",
        f"总体结论 (overall conclusion): {report['conclusion']}",
    ]
    if report["conclusion"] == UNVERIFIABLE:
        lines.append("  存在阻碍完整比较的问题; 本报告不是完整的兼容证明, "
                     "请先解决下方列出的问题。")
    elif report["conclusion"] == INCOMPATIBLE:
        lines.append("  新旧布局对相同字节给出不同解释; 受影响的日期区间"
                     "需要发布评审。")
    else:
        lines.append("  所有受影响日期区间内, 既有字段的字节位置与解释"
                     "保持一致。")
    lines.append("")

    status_by_sender = {block["sender"]: block["status"]
                        for block in report["senders"]}
    senders: dict = {}
    for key_result in key_results:
        senders.setdefault(key_result["sender"], []).append(key_result)
    for sender in sorted(senders):
        lines.append(f"发送方 {sender} — 状态: {status_by_sender[sender]}")
        for key in senders[sender]:
            lines.append(f"  copybook {key['copybook']}:")
            if key["affected_ranges"]:
                lines.append("    受影响日期区间 (布局版本发生变化):")
                for rng in key["affected_ranges"]:
                    lines.append(
                        f"      {_range_text(rng)}  "
                        f"v{rng['old_version']} -> v{rng['new_version']}"
                        f"  结论: {rng['conclusion']}")
            if key["unaffected_ranges"]:
                lines.append("    不受影响 (两侧选择同一版本且布局一致):")
                for rng in key["unaffected_ranges"]:
                    lines.append(f"      {_range_text(rng)}  "
                                 f"v{rng['version']}")
            if key["unresolvable_ranges"]:
                lines.append("    无法判定 (见问题列表):")
                for rng in key["unresolvable_ranges"]:
                    lines.append(f"      {_range_text(rng)}  "
                                 f"原因: {rng['reason']}")
            for i, cmp in enumerate(key["comparisons"]):
                applies = [r for r in key["affected_ranges"]
                           if r["comparison"] == i]
                for rng in applies:
                    lines.append(f"    适用区间: {_range_text(rng)}")
                _render_comparison(lines, cmp)
        lines.append("")

    lines.append(f"问题列表 ({report['problem_count']}):")
    if not report["problems"]:
        lines.append("  (无)")
    for prob in report["problems"]:
        scope = []
        if prob.get("sender"):
            scope.append(f"sender={prob['sender']}")
        if prob.get("copybook"):
            scope.append(f"copybook={prob['copybook']}")
        if prob.get("range"):
            scope.append(f"日期 {_range_text(prob['range'])}")
        where = " ".join(scope) if scope else "(清单级)"
        lines.append(f"  [{prob['kind']}] 来源={prob['source']} {where}")
        lines.append(f"      {prob['message']}")
    lines.append("")
    lines.append("说明: 字节区间为半开区间 [start, end); 生效日期为闭区间,"
                 " '开放' 表示无 effective_to 上限。REDEFINES 视图是同一"
                 "字节的互斥解释, 不同时占用存储。")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- engine

def run_impact_report(old_manifest: Union[str, Path],
                      new_manifest: Union[str, Path],
                      output_dir: Union[str, Path]) -> dict:
    """Compare two receiving manifests and write the impact report.

    The output directory must not exist yet; an existing directory is
    never overwritten (:class:`ImpactError`).  The directory always
    receives a human-readable ``impact_report.txt`` and a
    machine-readable ``impact_report.json`` holding the field-level
    diff.  Returns a bounded summary dict.
    """
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ImpactError(
            f"output directory {output_dir} already exists; refusing to "
            "overwrite an existing impact report")

    problems: list = []
    old_side = _load_side(old_manifest, "old_manifest", problems)
    new_side = _load_side(new_manifest, "new_manifest", problems)

    keys = sorted({(e.sender, e.copybook) for e in old_side.entries}
                  | {(e.sender, e.copybook) for e in new_side.entries})
    key_results = [_analyse_key(sender, copybook, old_side, new_side,
                                problems)
                   for sender, copybook in keys]

    senders: dict = {}
    for key_result in key_results:
        senders.setdefault(key_result["sender"], []).append(key_result)

    sender_blocks = []
    for sender in sorted(senders):
        blocks = senders[sender]
        status = max((_key_status(k) for k in blocks),
                     key=lambda s: _STATUS_ORDER[s])
        affected = []
        for key in blocks:
            for rng in key["affected_ranges"]:
                affected.append({"copybook": key["copybook"], **rng})
        sender_blocks.append({
            "sender": sender,
            "status": status,
            "affected_ranges": affected,
            "keys": [{
                "copybook": key["copybook"],
                "affected_ranges": key["affected_ranges"],
                "unaffected_ranges": key["unaffected_ranges"],
                "unresolvable_ranges": key["unresolvable_ranges"],
                "comparisons": [c.describe() for c in key["comparisons"]],
            } for key in blocks],
        })

    all_comparisons = [c for key in key_results
                       for c in key["comparisons"]]
    if problems or any(c.conclusion == UNVERIFIABLE
                       for c in all_comparisons):
        conclusion = UNVERIFIABLE
    elif any(c.conclusion == INCOMPATIBLE for c in all_comparisons):
        conclusion = INCOMPATIBLE
    else:
        conclusion = COMPATIBLE

    report = {
        "report": "layout-change-impact",
        "format_version": 1,
        "sources": {"old_manifest": str(Path(old_manifest)),
                    "new_manifest": str(Path(new_manifest))},
        "conclusion": conclusion,
        "problem_count": len(problems),
        "problems": problems,
        "senders": sender_blocks,
    }

    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ImpactError(
            f"output directory {output_dir} already exists; refusing to "
            "overwrite an existing impact report")
    (output_dir / REPORT_JSON).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (output_dir / REPORT_TXT).write_text(
        _render_text(report, key_results), encoding="utf-8")

    return {
        "conclusion": conclusion,
        "problem_count": len(problems),
        "sender_count": len(sender_blocks),
        "affected_range_count": sum(len(b["affected_ranges"])
                                    for b in sender_blocks),
        "incompatible_comparison_count": sum(
            1 for c in all_comparisons
            if c.conclusion == INCOMPATIBLE),
        "output_dir": str(output_dir),
        "report_file": REPORT_TXT,
        "diff_file": REPORT_JSON,
    }
