"""Masking engine: scan and validate everything, then deliver once.

The engine turns already-decoded JSONL records (the shape produced by
:meth:`DecodeResult.write_jsonl`) into a de-identified sample set plus a
report explaining how every record was handled.  It never re-reads the
original EBCDIC files, never compares layouts and never judges whether a
batch may enter the warehouse.

Run shape
---------

1. **Scan** every input file (streamed in bounded chunks): JSON and
   record-structure validation, policy/record type checks, collection of
   the distinct pseudonym values per link group into a SQLite staging
   database on disk.  Nothing is written to the output directory yet.
2. **Build** the pseudonym mappings: per-group JSON-type consistency,
   injective fake computation, collision detection.  A collision or a
   policy path that matched nothing aborts the run before any output
   exists.
3. **Deliver**: create the (previously non-existent) output directory and
   stream the inputs a second time, writing one masked JSONL per input, a
   per-record report, and the machine/human readable run reports.  If
   anything fails mid-delivery the half-written directory is removed
   again -- a run never leaves a seemingly complete dataset behind.

Determinism: masking is a pure function of (key, policy scope, original
value), and files are processed in canonical sorted order, so the output
is identical for any input file order and any chunk size.  Original
sensitive values and the reversible mapping table are never persisted:
the staging database is deleted when the run ends.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Union

from .errors import (MaskConflictError, MaskError, MaskInputError,
                     MaskPolicyError)
from .mapping import (MaskKey, bucketize_amount, bucketize_date,
                      pseudonym_value, shift_amount, shift_date,
                      synthesize_name)
from .policy import (BUCKETIZE, KIND_AMOUNT, PSEUDONYM, SHIFT,
                     SYNTHESIZE, FieldRule, MaskPolicy)

REPORT_JSON = "mask_report.json"
REPORT_TXT = "mask_report.txt"
RECORD_REPORT = "record_report.jsonl"

DEFAULT_CHUNK_SIZE = 5000

#: Boundary documentation, repeated into every report.
NOTES = (
    "稳定映射的输入范围: 算法版本 copybook-mask/v1、动作、关联组(未声明组时"
    "为完整字段路径)、规范化后的原值、映射密钥, 五者共同决定输出。",
    "同一密钥与策略下, 同一原值在任意输入文件、任意文件顺序、任意分块大小"
    "下得到同一假值; 不同关联组之间即使原值相同也互不可关联。",
    "密钥轮换后 pseudonym/synthesize/shift 的全部结果都会改变: 新交付无法与"
    "旧密钥产出的脱敏数据关联; 同一次运行内部仍可关联。bucketize 不依赖密钥,"
    "轮换前后结果一致。",
    "输出与报告不含原始敏感值, 也不含可逆映射表; 映射密钥只存在于内存, 临时"
    "暂存库在运行结束后删除。",
)

_AMOUNT_RE = re.compile(r"^[+-]?[0-9]+(\.[0-9]+)?$")


class _Reject(Exception):
    """Internal: the current record must be refused (policy on_null/on_empty)."""


# --------------------------------------------------------- value checking

def _normalize_pseudonym(rule: FieldRule, value: Any) -> tuple:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MaskPolicyError(
            f"rule {rule.path_text!r} (pseudonym) requires a string or a "
            f"non-negative integer value, got {type(value).__name__}")
    if isinstance(value, int):
        if value < 0:
            raise MaskPolicyError(
                f"rule {rule.path_text!r} (pseudonym) cannot map a "
                "negative integer; the fake must keep the original type "
                "and format")
        return str(value), "int"
    return value, "str"


def _normalize_amount(rule: FieldRule, value: Any) -> tuple:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MaskPolicyError(
            f"rule {rule.path_text!r} ({rule.action}/amount) requires an "
            f"integer or a plain decimal string, got "
            f"{type(value).__name__}")
    if isinstance(value, int):
        return Decimal(value), 0, True
    if not _AMOUNT_RE.match(value):
        raise MaskPolicyError(
            f"rule {rule.path_text!r} ({rule.action}/amount) requires a "
            "plain decimal string like '1234.50'; the value at this "
            "path is not one")
    parsed = Decimal(value)
    scale = max(0, -parsed.as_tuple().exponent)
    return parsed, scale, False


def _normalize_date(rule: FieldRule, value: Any) -> tuple:
    if not isinstance(value, str):
        raise MaskPolicyError(
            f"rule {rule.path_text!r} ({rule.action}/date) requires a "
            f"string matching format {rule.fmt!r}, got "
            f"{type(value).__name__}")
    try:
        day = datetime.strptime(value, rule.fmt).date()
    except ValueError:
        raise MaskPolicyError(
            f"rule {rule.path_text!r} ({rule.action}/date): the value "
            f"does not match date format {rule.fmt!r}")
    return day, value


def _classify_terminal(rule: FieldRule, value: Any) -> tuple:
    """Shared null/empty/type classification for scan and write.

    Returns ``("null", None)``, ``("empty", None)`` or
    ``("ok", payload)`` where *payload* is the normalized, type-checked
    value.  Raises :class:`MaskPolicyError` when the value's type does not
    fit the declared action -- such mismatches are never silently skipped.
    """
    if value is None:
        return "null", None
    if isinstance(value, str) and value == "":
        return "empty", None
    if rule.action == PSEUDONYM:
        return "ok", _normalize_pseudonym(rule, value)
    if rule.action == SYNTHESIZE:
        if not isinstance(value, str):
            raise MaskPolicyError(
                f"rule {rule.path_text!r} (synthesize) requires a string "
                f"value, got {type(value).__name__}")
        return "ok", value
    if rule.kind == KIND_AMOUNT:
        return "ok", _normalize_amount(rule, value)
    return "ok", _normalize_date(rule, value)


# --------------------------------------------------------------- traversal

def _apply(node: Any, parts: tuple, fn) -> tuple:
    """Apply *fn* to every terminal the path resolves to.

    Dict segments hop by key; when a segment meets a list the remaining
    path is applied to every element (OCCURS wildcard).  Returns
    ``(new_node, terminal_count)``; a count of 0 means the path was
    *missing* at this node.  A path resolving to a container, or crossing
    a scalar mid-way, is a policy error -- never silently skipped.
    """
    if not parts:
        if isinstance(node, (dict, list)):
            raise MaskPolicyError(
                "rule path resolves to a container (group or OCCURS "
                "list); policies address scalar fields only")
        return fn(node), 1
    head, rest = parts[0], parts[1:]
    if isinstance(node, dict):
        if head not in node:
            return node, 0
        new_child, count = _apply(node[head], rest, fn)
        if count == 0:
            return node, 0
        new_node = dict(node)
        new_node[head] = new_child
        return new_node, count
    if isinstance(node, list):
        total = 0
        out = []
        for element in node:
            new_element, count = _apply(element, parts, fn)
            total += count
            out.append(new_element if count else element)
        if total == 0:
            return node, 0
        return out, total
    raise MaskPolicyError(
        f"rule path {'/'.join(parts)!r} crosses a scalar value at "
        f"segment {head!r}; the policy expects a container there")


# ------------------------------------------------------------------ stats

@dataclass
class _RuleStats:
    matched_records: int = 0
    matched_values: int = 0
    nulls: int = 0
    empties: int = 0
    missing_records: int = 0


@dataclass
class _FileScan:
    path: Path
    records: int = 0


def _validate_record(obj: Any, path: Path, line_no: int) -> None:
    """Structural validation of one decoded-JSONL line."""
    where = f"{path}:{line_no}"
    if not isinstance(obj, dict):
        raise MaskInputError(
            f"{where}: expected a JSON object per line, got "
            f"{type(obj).__name__}")
    meta = obj.get("meta")
    if not isinstance(meta, dict):
        raise MaskInputError(f"{where}: record has no 'meta' object")
    source = meta.get("source_file")
    if not isinstance(source, str) or not source:
        raise MaskInputError(
            f"{where}: meta.source_file must be a non-empty string")
    line_number = meta.get("line_number")
    if isinstance(line_number, bool) or not isinstance(line_number, int) \
            or line_number < 1:
        raise MaskInputError(
            f"{where}: meta.line_number must be a positive integer")
    if not isinstance(obj.get("fields"), dict):
        raise MaskInputError(f"{where}: record has no 'fields' object")


def _iter_line_batches(path: Path, chunk_size: int):
    """Yield bounded batches of (line_number, raw_text) from *path*."""
    with path.open("r", encoding="utf-8") as handle:
        batch = []
        for line_no, raw in enumerate(handle, 1):
            batch.append((line_no, raw))
            if len(batch) >= chunk_size:
                yield batch
                batch = []
        if batch:
            yield batch


# ------------------------------------------------------------------ engine

def _scan_file(path: Path, policy: MaskPolicy, rule_stats: list,
               conn: sqlite3.Connection, chunk_size: int) -> _FileScan:
    """Validate one input file and collect distinct pseudonym values."""
    scan = _FileScan(path)
    for batch in _iter_line_batches(path, chunk_size):
        pending = []
        for line_no, raw in batch:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MaskInputError(
                    f"{path}:{line_no}: invalid JSON: {exc}")
            _validate_record(obj, path, line_no)
            scan.records += 1
            fields = obj["fields"]
            for rule, stats in zip(policy.rules, rule_stats):
                def collect(value, _rule=rule, _stats=stats):
                    cls, payload = _classify_terminal(_rule, value)
                    if cls == "null":
                        _stats.nulls += 1
                    elif cls == "empty":
                        _stats.empties += 1
                    else:
                        _stats.matched_values += 1
                        if _rule.action == PSEUDONYM:
                            text, json_type = payload
                            pending.append(
                                (_rule.group, json_type, text))
                    return value

                try:
                    _, count = _apply(fields, rule.path, collect)
                except MaskPolicyError as exc:
                    raise MaskPolicyError(
                        f"{path}:{line_no}: {exc}") from exc
                if count:
                    stats.matched_records += 1
                else:
                    stats.missing_records += 1
        if pending:
            conn.executemany(
                "INSERT OR IGNORE INTO distinct_values"
                " (group_name, json_type, original) VALUES (?, ?, ?)",
                pending)
            conn.commit()
    return scan


def _build_mappings(policy: MaskPolicy, key: MaskKey,
                    conn: sqlite3.Connection) -> list:
    """Compute injective pseudonym fakes per group; detect conflicts."""
    mixed = [row[0] for row in conn.execute(
        "SELECT group_name FROM distinct_values GROUP BY group_name "
        "HAVING COUNT(DISTINCT json_type) > 1")]
    if mixed:
        raise MaskPolicyError(
            f"pseudonym group(s) {mixed} mix string and integer account "
            "representations; a stable cross-file mapping cannot be "
            "guaranteed -- align the field types or split the groups")

    groups = {}
    cursor = conn.execute(
        "SELECT group_name, json_type, original FROM distinct_values "
        "ORDER BY group_name, original")
    batch = []
    for group_name, json_type, original in cursor:
        fake = pseudonym_value(key.material, group_name, original,
                               no_leading_zero=(json_type == "int"))
        batch.append((group_name, original, fake))
        info = groups.setdefault(group_name, {"distinct": 0,
                                              "json_type": json_type})
        info["distinct"] += 1
        if len(batch) >= 2000:
            conn.executemany(
                "INSERT INTO fakes (group_name, original, fake)"
                " VALUES (?, ?, ?)", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT INTO fakes (group_name, original, fake)"
            " VALUES (?, ?, ?)", batch)
        conn.commit()

    conflict = conn.execute(
        "SELECT group_name, fake, COUNT(DISTINCT original) AS c "
        "FROM fakes GROUP BY group_name, fake HAVING c > 1 "
        "ORDER BY group_name LIMIT 1").fetchone()
    if conflict is not None:
        group_name, fake, count = conflict
        raise MaskConflictError(
            f"pseudonym group {group_name!r}: {count} distinct original "
            f"values collapse onto the same fake {fake!r}; the run "
            "refuses to merge identities -- use a wider field format or "
            "split the group")

    ordered = []
    seen_groups = set()
    for rule in policy.rules:
        if rule.action == PSEUDONYM and rule.group not in seen_groups:
            seen_groups.add(rule.group)
            info = groups.get(rule.group)
            ordered.append({
                "group": rule.group,
                "distinct_values": info["distinct"] if info else 0,
                "json_type": info["json_type"] if info else None,
                "collision_check": "passed",
            })
    return ordered


def _make_write_fn(rule: FieldRule, key: MaskKey, recorder: dict):
    def fn(value):
        cls, payload = _classify_terminal(rule, value)
        if cls == "null":
            recorder["nulls"] += 1
            if rule.on_null == "reject":
                raise _Reject(
                    f"null value at path {rule.path_text!r} refused by "
                    "policy (on_null=reject)")
            return value
        if cls == "empty":
            recorder["empties"] += 1
            if rule.on_empty == "reject":
                raise _Reject(
                    f"empty string at path {rule.path_text!r} refused by "
                    "policy (on_empty=reject)")
            return value
        recorder["values"] += 1
        if rule.action == PSEUDONYM:
            text, json_type = payload
            fake = pseudonym_value(key.material, rule.group, text,
                                   no_leading_zero=(json_type == "int"))
            return int(fake) if json_type == "int" else fake
        if rule.action == SYNTHESIZE:
            return synthesize_name(key.material, rule.scope, payload)
        if rule.action == SHIFT:
            if rule.kind == KIND_AMOUNT:
                amount, scale, was_int = payload
                shifted = shift_amount(key.material, rule.scope, amount,
                                       scale, max_pct=rule.max_pct,
                                       max_abs=rule.max_abs)
                if was_int:
                    return int(shifted)
                return str(shifted)
            day, original_text = payload
            return shift_date(key.material, rule.scope, day,
                              original_text,
                              max_days=rule.max_days).strftime(rule.fmt)
        # BUCKETIZE
        if rule.kind == KIND_AMOUNT:
            amount, _scale, _was_int = payload
            return bucketize_amount(amount, rule.boundaries)
        day, _original_text = payload
        return bucketize_date(day, rule.granularity)
    return fn


def _write_file(path: Path, out_path: Path, policy: MaskPolicy,
                key: MaskKey, chunk_size: int, report_handle,
                rejections: list) -> dict:
    """Mask one input file; append per-record lines to the record report."""
    counts = {"input": str(path), "output": out_path.name,
              "records_in": 0, "records_out": 0, "records_rejected": 0}
    with out_path.open("w", encoding="utf-8") as out:
        for batch in _iter_line_batches(path, chunk_size):
            for line_no, raw in batch:
                obj = json.loads(raw)          # already validated in scan
                counts["records_in"] += 1
                fields = obj["fields"]
                new_fields = fields
                outcomes = []
                rejection = None
                for rule in policy.rules:
                    recorder = {"values": 0, "nulls": 0, "empties": 0}
                    fn = _make_write_fn(rule, key, recorder)
                    try:
                        new_fields, count = _apply(new_fields, rule.path,
                                                   fn)
                    except _Reject as rej:
                        rejection = (rule, str(rej))
                        break
                    if count == 0:
                        outcomes.append({"path": list(rule.path),
                                         "result": "missing",
                                         "terminals": 0})
                    elif recorder["values"]:
                        outcomes.append({"path": list(rule.path),
                                         "result": rule.action,
                                         "terminals": count})
                    elif recorder["nulls"]:
                        outcomes.append({"path": list(rule.path),
                                         "result": "null_kept",
                                         "terminals": count})
                    else:
                        outcomes.append({"path": list(rule.path),
                                         "result": "empty_kept",
                                         "terminals": count})
                meta = obj["meta"]
                report_line = {"file": str(path),
                               "line_number": meta["line_number"]}
                if rejection is not None:
                    rule, reason = rejection
                    counts["records_rejected"] += 1
                    rejections.append({
                        "file": str(path),
                        "line_number": meta["line_number"],
                        "path": list(rule.path),
                        "reason": reason,
                    })
                    report_line["status"] = "rejected"
                    report_line["reason"] = reason
                    report_line["rejected_by"] = list(rule.path)
                else:
                    counts["records_out"] += 1
                    masked = dict(obj)
                    masked["fields"] = new_fields
                    out.write(json.dumps(masked) + "\n")
                    report_line["status"] = (
                        "masked" if any(o["result"] in
                                        (PSEUDONYM, SYNTHESIZE, SHIFT,
                                         BUCKETIZE)
                                        for o in outcomes) else "kept")
                    report_line["outcomes"] = outcomes
                report_handle.write(json.dumps(report_line) + "\n")
    if counts["records_in"] != counts["records_out"] + \
            counts["records_rejected"]:
        raise MaskError(  # pragma: no cover - internal consistency
            f"internal count mismatch for {path}")
    return counts


def _render_text(report: dict) -> str:
    lines = [
        "脱敏样本生成报告 / Masked Sample Generation Report",
        "=" * 52,
        f"输入文件 ({len(report['inputs'])}):",
    ]
    for item in report["inputs"]:
        lines.append(f"  {item}")
    lines += [
        f"密钥指纹 (key fingerprint): {report['key_fingerprint']}",
        f"结论: {report['conclusion']}",
        "",
        "逐文件计数 (per-file counts):",
        f"  {'输入文件':<40} {'读入':>8} {'交付':>8} {'拒绝':>8}",
    ]
    for f in report["files"]:
        lines.append(f"  {f['input']:<40} {f['records_in']:>8} "
                     f"{f['records_out']:>8} {f['records_rejected']:>8}")
    totals = report["totals"]
    lines.append(f"  {'合计':<40} {totals['records_in']:>8} "
                 f"{totals['records_out']:>8} "
                 f"{totals['records_rejected']:>8}")
    lines.append("")
    lines.append("策略摘要 (policy summary):")
    for rule in report["policy"]["rules"]:
        group = f" group={rule['group']}" if rule.get("group") else ""
        lines.append(
            f"  {rule['path_text']}  action={rule['action']} "
            f"kind={rule['kind']}{group}")
        lines.append(
            f"      命中记录 {rule['matched_records']} (值 "
            f"{rule['matched_values']}), 空值 {rule['nulls']}, 空串 "
            f"{rule['empties']}, 缺字段 {rule['missing_records']}")
    if report["policy"]["groups"]:
        lines.append("关联组 (link groups):")
        for grp in report["policy"]["groups"]:
            lines.append(
                f"  {grp['group']}: 不同原值 {grp['distinct_values']} 个, "
                f"类型 {grp['json_type']}, 冲突检查 "
                f"{grp['collision_check']}")
    lines.append("")
    lines.append(f"拒绝记录 ({len(report['rejections'])}):")
    if not report["rejections"]:
        lines.append("  (无)")
    for rej in report["rejections"][:100]:
        lines.append(f"  {rej['file']}:{rej['line_number']}  "
                     f"路径 {'/'.join(rej['path'])}  原因: {rej['reason']}")
    if len(report["rejections"]) > 100:
        lines.append(f"  ... 其余 {len(report['rejections']) - 100} 条见 "
                     f"{REPORT_JSON}")
    lines.append("")
    lines.append("边界说明 (mapping scope and rotation boundary):")
    for note in report["notes"]:
        lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def run_mask(inputs, *, policy: MaskPolicy, key: MaskKey,
             output_dir: Union[str, Path],
             chunk_size: int = DEFAULT_CHUNK_SIZE,
             temp_dir: Optional[Union[str, Path]] = None) -> dict:
    """Mask decoded JSONL files into a new output directory.

    *inputs* is a non-empty list of decoded-record JSONL files.  The
    *output_dir* must not exist yet.  All inputs and the policy are
    scanned and validated before anything is delivered; corrupt input, an
    unusable key, a mapping conflict or an existing output path abort the
    run without leaving a seemingly complete dataset behind.  Returns a
    bounded summary dict.
    """
    if not inputs:
        raise MaskInputError("at least one input JSONL file is required")
    if isinstance(chunk_size, int) and chunk_size >= 1:
        pass
    else:
        raise MaskError(f"chunk_size must be an integer >= 1, got "
                        f"{chunk_size!r}")

    paths = [Path(p) for p in inputs]
    if len(set(paths)) != len(paths):
        raise MaskInputError("duplicate input files are not allowed")
    for p in paths:
        if not p.is_file():
            raise MaskInputError(f"input file {p} does not exist or is "
                                 "not a regular file")
    stems = [p.stem for p in paths]
    if len(set(stems)) != len(stems):
        raise MaskInputError(
            f"input files share a stem {sorted(stems)}; output names "
            "would be ambiguous")

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise MaskError(
            f"output directory {output_dir} already exists; refusing to "
            "overwrite an existing delivery")

    key.check_fingerprint(policy.key_fingerprint)

    # Canonical processing order: results must not depend on the order in
    # which the caller listed the inputs.
    paths = sorted(paths, key=str)

    staging_base = Path(temp_dir) if temp_dir is not None else None
    if staging_base is not None:
        staging_base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="mask-staging-",
                                    dir=staging_base))
    created_output = False
    try:
        conn = sqlite3.connect(staging / "mask.sqlite3")
        try:
            conn.executescript(
                """
                CREATE TABLE distinct_values (
                    group_name TEXT NOT NULL,
                    json_type  TEXT NOT NULL,
                    original   TEXT NOT NULL,
                    PRIMARY KEY (group_name, json_type, original)
                );
                CREATE TABLE fakes (
                    group_name TEXT NOT NULL,
                    original   TEXT NOT NULL,
                    fake       TEXT NOT NULL,
                    PRIMARY KEY (group_name, original)
                );
                CREATE INDEX fakes_by_fake ON fakes (group_name, fake);
                """)

            # ---- phase 1+2: scan & validate everything, build mappings
            rule_stats = [_RuleStats() for _ in policy.rules]
            scans = [_scan_file(p, policy, rule_stats, conn, chunk_size)
                     for p in paths]
            groups = _build_mappings(policy, key, conn)
            never_matched = [rule.path_text for rule, stats in
                             zip(policy.rules, rule_stats)
                             if stats.matched_records == 0]
            if never_matched:
                raise MaskPolicyError(
                    f"policy path(s) {never_matched} never matched any "
                    "record in any input file; fix the policy instead of "
                    "silently ignoring the rule")

            # ---- phase 3: deliver
            try:
                output_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise MaskError(
                    f"output directory {output_dir} already exists; "
                    "refusing to overwrite an existing delivery")
            created_output = True

            file_counts = []
            rejections: list = []
            with (output_dir / RECORD_REPORT).open("w",
                                                   encoding="utf-8") as rh:
                for p, scan in zip(paths, scans):
                    out_path = output_dir / f"{p.stem}.masked.jsonl"
                    counts = _write_file(p, out_path, policy, key,
                                         chunk_size, rh, rejections)
                    if counts["records_in"] != scan.records:
                        raise MaskInputError(
                            f"input file {p} changed between the scan "
                            f"and delivery phases ({scan.records} -> "
                            f"{counts['records_in']} records); refusing "
                            "to deliver an inconsistent dataset")
                    file_counts.append(counts)

            totals = {
                "records_in": sum(f["records_in"] for f in file_counts),
                "records_out": sum(f["records_out"] for f in file_counts),
                "records_rejected": sum(f["records_rejected"]
                                        for f in file_counts),
            }
            report = {
                "report": "mask-run",
                "format_version": 1,
                "conclusion": "masked",
                "key_fingerprint": key.fingerprint,
                "inputs": [str(p) for p in paths],
                "outputs": {
                    "records": [f["output"] for f in file_counts],
                    "record_report": RECORD_REPORT,
                    "report_json": REPORT_JSON,
                    "report_text": REPORT_TXT,
                },
                "files": file_counts,
                "totals": totals,
                "policy": {
                    "rules": [
                        {
                            "path": list(rule.path),
                            "path_text": rule.path_text,
                            "action": rule.action,
                            "kind": rule.kind,
                            "group": rule.group,
                            "on_null": rule.on_null,
                            "on_empty": rule.on_empty,
                            "matched_records": stats.matched_records,
                            "matched_values": stats.matched_values,
                            "nulls": stats.nulls,
                            "empties": stats.empties,
                            "missing_records": stats.missing_records,
                        }
                        for rule, stats in zip(policy.rules, rule_stats)
                    ],
                    "groups": groups,
                },
                "rejections": rejections,
                "notes": list(NOTES),
            }
            (output_dir / REPORT_JSON).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            (output_dir / REPORT_TXT).write_text(
                _render_text(report), encoding="utf-8")
        finally:
            conn.close()
    except Exception:
        if created_output:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "conclusion": "masked",
        "file_count": len(file_counts),
        "records_in": totals["records_in"],
        "records_out": totals["records_out"],
        "records_rejected": totals["records_rejected"],
        "rule_count": len(policy.rules),
        "group_count": len(groups),
        "distinct_pseudonym_values": sum(g["distinct_values"]
                                         for g in groups),
        "output_dir": str(output_dir),
        "report_json": REPORT_JSON,
        "report_text": REPORT_TXT,
        "record_report": RECORD_REPORT,
    }
