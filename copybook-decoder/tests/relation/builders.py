"""Fixtures and builders for the relational admission tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def wrap(row_id: Any, fields: Optional[dict] = None, *,
         source_file: str = "t.jsonl", line_number: int = 1,
         byte_start: int = 0, byte_end: Optional[int] = None,
         rid_field: str = "id") -> dict:
    """Build one meta/fields-wrapped decoded record."""
    fields = dict(fields or {})
    if row_id is not None and rid_field not in fields:
        fields[rid_field] = row_id
    if byte_end is None:
        byte_end = byte_start + 40
    return {
        "meta": {"source_file": source_file, "line_number": line_number,
                 "byte_start": byte_start, "byte_end": byte_end},
        "fields": fields,
    }


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def field(path: list[str], col_type: str = "string",
          nullable: bool = False) -> dict:
    return {"path": path, "type": col_type, "nullable": nullable}


def table(name: str, files: list[str], fields: dict, *,
          row_id_path: Optional[list[str]] = None,
          candidate_keys: Optional[dict] = None,
          references: Optional[list[dict]] = None,
          rid_field: str = "id") -> dict:
    return {
        "name": name, "files": files,
        "row_id_path": row_id_path or [rid_field],
        "fields": fields,
        "candidate_keys": candidate_keys or {},
        "references": references or [],
    }


def ref(name: str, columns: list[str], target_table: str, target_key: str,
        rule: str = "mandatory") -> dict:
    return {"name": name, "columns": columns,
            "target_table": target_table, "target_key": target_key,
            "rule": rule}


def write_contract(path: Path, tables: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tables": tables}, ensure_ascii=False,
                               indent=2), encoding="utf-8")
    return path


def status_map(db) -> dict[tuple[str, str], tuple[str, Optional[int]]]:
    """Dump (table ordinal, row_id) -> (status, layer) for assertions."""
    out = {}
    for ordinal, row_id, status, layer in db.execute(
            "SELECT table_ord, row_id, status, layer FROM records"):
        out[(ordinal, row_id)] = (status, layer)
    return out
