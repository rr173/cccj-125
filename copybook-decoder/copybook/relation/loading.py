"""Streaming input load and per-row local validation.

The loader is the only component that reads the input JSONL files.  It
walks them one physical line at a time and stages everything into the
SQLite database; it never keeps a whole table in Python memory.

Two failure classes are kept strictly apart:

* **structural input faults** -- unreadable files, blank lines, bad JSON,
  duplicate JSON object members, an invalid ``meta`` / ``fields`` wrapper,
  missing/empty row ids, duplicate row ids, invalid meta offsets -- make
  the whole handoff unprocessable and raise :class:`RelationInputError`;
* **local business faults** -- a missing declared column, a null in a
  non-nullable column, a type mismatch, a half-null optional reference --
  are diagnostics attached to exactly one row (layer 0).  Such a row is
  kept on record with its origin, but it can never support another row.

Undeclared fields are carried through untouched, and the original
``meta`` / ``fields`` wrapper plus the input file and physical line number
are preserved verbatim; the raw EBCDIC is never decoded again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .config import (INTEGER, RelationContract, TableContract,
                     encode_tuple, json_loads_strict)
from .errors import RelationInputError

#: Flush a bounded batch of staged rows so the write window never scales
#: with file size; mirrors the audit engine's batching discipline.
WRITE_BATCH = 2000


def _where(source_file: str, line_number: Optional[int]) -> str:
    if line_number is None:
        return str(source_file)
    return f"{source_file} line {line_number}"


def _is_plain_int(value: Any) -> bool:
    """JSON integer test that rejects booleans (``bool`` is an ``int``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def validate_meta(meta: Any, where: str) -> dict:
    """Validate the mandatory meta envelope of one record."""
    if not isinstance(meta, dict):
        raise RelationInputError(f"{where}: 'meta' must be an object")
    source_file = meta.get("source_file")
    if not isinstance(source_file, str) or not source_file:
        raise RelationInputError(
            f"{where}: meta.source_file must be a non-empty string")
    line_number = meta.get("line_number")
    if not _is_plain_int(line_number) or line_number <= 0:
        raise RelationInputError(
            f"{where}: meta.line_number must be a positive integer")
    byte_start = meta.get("byte_start")
    byte_end = meta.get("byte_end")
    for name in ("byte_start", "byte_end"):
        value = meta.get(name)
        if not _is_plain_int(value) or value < 0:
            raise RelationInputError(
                f"{where}: meta.{name} must be a non-negative integer")
    if not byte_start < byte_end:
        raise RelationInputError(
            f"{where}: meta.byte_start/byte_end must form a legal "
            "half-open range (byte_start < byte_end)")
    return meta


class StagingStore:
    """Schema and append helpers for the on-disk staging database."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self._uncommitted = 0
        self._create()

    def _create(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE records (
                table_ord   INTEGER NOT NULL,
                row_id      TEXT NOT NULL,
                source_file TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                payload     TEXT NOT NULL,           -- verbatim JSON object
                status      TEXT NOT NULL,           -- see engine statuses
                layer       INTEGER,
                input_ord   INTEGER NOT NULL,
                PRIMARY KEY (table_ord, row_id)
            );
            CREATE TABLE local_diags (
                table_ord   INTEGER NOT NULL,
                row_id      TEXT NOT NULL,
                code        TEXT NOT NULL,
                message     TEXT NOT NULL,
                detail      TEXT NOT NULL,
                diag_ord    INTEGER NOT NULL,
                PRIMARY KEY (table_ord, row_id, diag_ord)
            );
            -- One row per (record, candidate key) whose tuple reads cleanly.
            CREATE TABLE key_values (
                table_ord   INTEGER NOT NULL,
                key_name    TEXT NOT NULL,
                key_tuple   TEXT NOT NULL,
                row_id      TEXT NOT NULL,
                PRIMARY KEY (table_ord, key_name, row_id)
            );
            -- One row per reference edge of a locally valid row.  Edge
            -- liveness is purely a function of the target tuple, so every
            -- edge sharing a tuple always flips together; dead_round is the
            -- synchronous round (1-based) in which support vanished.
            CREATE TABLE ref_edges (
                edge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                table_ord      INTEGER NOT NULL,
                row_id         TEXT NOT NULL,
                ref_name       TEXT NOT NULL,
                target_ord     INTEGER NOT NULL,
                target_key     TEXT NOT NULL,
                key_tuple      TEXT NOT NULL,
                is_optional    INTEGER NOT NULL,
                live           INTEGER NOT NULL DEFAULT 1,
                dead_round     INTEGER,
                UNIQUE (table_ord, row_id, ref_name)
            );
            -- One row per distinct unsupported (target, key) group; edges
            -- collapse onto it so shared root causes are stored once.
            -- A group is a *proof* unit; edge liveness stays per edge in
            -- ref_edges because two owners sharing a target tuple do not
            -- lose support at the same moment.
            CREATE TABLE edge_groups (
                group_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                table_ord      INTEGER NOT NULL,
                ref_name       TEXT NOT NULL,
                target_ord     INTEGER NOT NULL,
                target_key     TEXT NOT NULL,
                key_tuple      TEXT NOT NULL,
                UNIQUE (table_ord, ref_name, target_ord, target_key,
                        key_tuple)
            );
            -- All-null optional references that need no parent at all.
            CREATE TABLE ref_skips (
                table_ord   INTEGER NOT NULL,
                row_id      TEXT NOT NULL,
                ref_name    TEXT NOT NULL,
                PRIMARY KEY (table_ord, row_id, ref_name)
            );
            CREATE TABLE proofs (
                proof_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        INTEGER NOT NULL UNIQUE,
                reject_layer    INTEGER NOT NULL
            );
            CREATE TABLE proof_candidates (
                proof_id        INTEGER NOT NULL,
                parent_ord      INTEGER NOT NULL,
                parent_row_id   TEXT NOT NULL,
                parent_layer    INTEGER NOT NULL,
                cand_ord        INTEGER NOT NULL,
                PRIMARY KEY (proof_id, cand_ord)
            );
            CREATE TABLE row_proofs (
                table_ord   INTEGER NOT NULL,
                row_id      TEXT NOT NULL,
                proof_id    INTEGER NOT NULL,
                PRIMARY KEY (table_ord, row_id)
            );
            CREATE TABLE witnesses (
                table_ord   INTEGER NOT NULL,
                row_id      TEXT NOT NULL,
                ref_name    TEXT NOT NULL,
                parent_ord  INTEGER NOT NULL,
                parent_row_id TEXT NOT NULL,
                PRIMARY KEY (table_ord, row_id, ref_name)
            );
            CREATE INDEX idx_records_status ON records(table_ord, status);
            CREATE INDEX idx_key_lookup
                ON key_values(table_ord, key_name, key_tuple, row_id);
            CREATE INDEX idx_key_owner
                ON key_values(table_ord, row_id);
            CREATE INDEX idx_edges_target
                ON ref_edges(target_ord, target_key, key_tuple, live);
            CREATE INDEX idx_edges_dead ON ref_edges(dead_round);
            CREATE INDEX idx_edges_owner ON ref_edges(table_ord, row_id);
            CREATE INDEX idx_diag_row ON local_diags(table_ord, row_id);
            """)

    def note_write(self) -> None:
        self._uncommitted += 1
        if self._uncommitted >= WRITE_BATCH:
            self.db.commit()
            self._uncommitted = 0

    def checkpoint(self) -> None:
        self.db.commit()
        self._uncommitted = 0


def load_inputs(db: sqlite3.Connection, contract: RelationContract) -> int:
    """Read every declared input file and stage rows with local verdicts.

    Returns the total number of input identities staged.  Raises
    :class:`RelationInputError` on any structural fault; in that case the
    caller aborts and no delivery directory is ever created.
    """
    store = StagingStore(db)
    seen_files: set[str] = set()
    input_ord = 0
    for table_name in contract.table_order:
        table = contract.tables[table_name]
        for file_name in table.files:
            if file_name in seen_files:
                raise RelationInputError(
                    f"input file {file_name!r} is declared by more than "
                    "one table; every input identity needs one owner")
            seen_files.add(file_name)
            input_ord = _load_one_file(store, table, file_name, input_ord)
    store.checkpoint()
    return store.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def _load_one_file(store: StagingStore, table: TableContract,
                   file_name: str, input_ord: int) -> int:
    path = Path(file_name)
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise RelationInputError(
            f"cannot open input file {path}: {exc}")
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise RelationInputError(
                    f"{_where(str(path), line_number)}: blank lines are "
                    "not legal handoff records")
            where = _where(str(path), line_number)
            try:
                obj = json_loads_strict(line, str(path))
            except json.JSONDecodeError as exc:
                raise RelationInputError(f"{where}: invalid JSON: {exc}")
            input_ord += 1
            _stage_record(store, table, obj, str(path), line_number,
                          input_ord, where)
    return input_ord


def _stage_record(store: StagingStore, table: TableContract, obj: Any,
                  source_file: str, line_number: int, input_ord: int,
                  where: str) -> None:
    if not isinstance(obj, dict):
        raise RelationInputError(
            f"{where}: every handoff record must be a JSON object")
    fields = obj.get("fields")
    if not isinstance(fields, dict):
        raise RelationInputError(
            f"{where}: record needs a 'fields' object")
    meta = validate_meta(obj.get("meta"), where)

    row_id = table.row_id_path.get(fields)
    if not isinstance(row_id, str) or not row_id:
        raise RelationInputError(
            f"{where}: row_id path "
            f"{'.'.join(table.row_id_path.parts)!r} must resolve to a "
            "non-empty string unique within the table")
    try:
        store.db.execute(
            "INSERT INTO records (table_ord, row_id, source_file, "
            "line_number, payload, status, input_ord) VALUES (?,?,?,?,?,"
            "'pending', ?)",
            (table.ordinal, row_id, source_file, line_number,
             json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
             input_ord))
    except sqlite3.IntegrityError:
        previous = store.db.execute(
            "SELECT source_file, line_number FROM records "
            "WHERE table_ord = ? AND row_id = ?",
            (table.ordinal, row_id)).fetchone()
        prev = _where(previous[0], previous[1]) if previous else "an earlier "
        raise RelationInputError(
            f"{where}: duplicate row_id {row_id!r} in table "
            f"{table.name!r}; first seen at {prev}. Identical content does "
            "not make a repeated identity legal")
    store.note_write()

    diagnostics = _local_field_diagnostics(table, fields)
    for diag_ord, (code, message, detail) in enumerate(diagnostics):
        store.db.execute(
            "INSERT INTO local_diags (table_ord, row_id, code, message, "
            "detail, diag_ord) VALUES (?,?,?,?,?,?)",
            (table.ordinal, row_id, code, message,
             json.dumps(detail, ensure_ascii=False), diag_ord))
        store.note_write()

    # Readable candidate key tuples are staged even for locally rejected
    # rows: a layer-0 row can never *support* anyone (its own references
    # are never evaluated), but its still-readable key remains an initial
    # parent candidate, exactly as the proof definition requires. A key
    # whose own columns are null or mistyped simply yields no tuple and so
    # never poses as a candidate for a known key.
    _stage_keys(store, table, fields, row_id)

    if diagnostics:
        # A row with any local field error has no support capability at
        # all: its references are never evaluated and produce no proof.
        store.db.execute(
            "UPDATE records SET status = 'local_reject' WHERE table_ord = ? "
            "AND row_id = ?", (table.ordinal, row_id))
        store.note_write()
        return

    _stage_references(store, table, fields, row_id)


def _local_field_diagnostics(table: TableContract,
                             fields: dict) -> list[tuple[str, str, dict]]:
    diags: list[tuple[str, str, dict]] = []
    for col_name, decl in table.fields.items():
        value = decl.path.get(fields)
        if value is None:
            if decl.nullable:
                continue
            diags.append((
                "null_or_missing_column",
                f"column {col_name!r} is not nullable and its path "
                f"{decl.path.spec()!r} is missing or null",
                {"column": col_name, "path": decl.path.spec()}))
            continue
        if decl.type == INTEGER:
            if not _is_plain_int(value):
                diags.append((
                    "type_mismatch",
                    f"column {col_name!r} expects integer, got "
                    f"{type(value).__name__} (booleans are not integers)",
                    {"column": col_name, "expected": "integer",
                     "actual_type": type(value).__name__}))
        else:
            if not isinstance(value, str):
                diags.append((
                    "type_mismatch",
                    f"column {col_name!r} expects string, got "
                    f"{type(value).__name__}",
                    {"column": col_name, "expected": "string",
                     "actual_type": type(value).__name__}))
    return diags


def _stage_keys(store: StagingStore, table: TableContract, fields: dict,
                row_id: str) -> None:
    for key_name, columns in table.candidate_keys.items():
        values: list[Any] = []
        types: list[str] = []
        readable = True
        for col_name in columns:
            decl = table.fields[col_name]
            value = decl.path.get(fields)
            # Key columns are contractually non-nullable and the row passed
            # local validation, so null here means the key cannot be read.
            if value is None or (decl.type == INTEGER
                                 and not _is_plain_int(value)) \
                    or (decl.type == "string"
                        and not isinstance(value, str)):
                readable = False
                break
            values.append(value)
            types.append(decl.type)
        if not readable:
            # An unreadable target key never poses as a candidate for a
            # known tuple; it simply contributes no key_values row.
            continue
        tuple_text = encode_tuple(values, tuple(types))
        store.db.execute(
            "INSERT INTO key_values (table_ord, key_name, key_tuple, "
            "row_id) VALUES (?,?,?,?)",
            (table.ordinal, key_name, tuple_text, row_id))
        store.note_write()


def _stage_references(store: StagingStore, table: TableContract,
                      fields: dict, row_id: str) -> None:
    for ref in table.references:
        values: list[Any] = []
        types: list[str] = []
        null_flags: list[bool] = []
        for col_name in ref.columns:
            decl = table.fields[col_name]
            value = decl.path.get(fields)
            values.append(value)
            types.append(decl.type)
            null_flags.append(value is None)
        all_null = all(null_flags)
        any_null = any(null_flags)
        if ref.optional and all_null:
            # Explicitly exempt: an all-null optional tuple needs no parent.
            store.db.execute(
                "INSERT INTO ref_skips (table_ord, row_id, ref_name) "
                "VALUES (?,?,?)", (table.ordinal, row_id, ref.name))
            store.note_write()
            continue
        if ref.optional and any_null:
            # Optional references may skip only when EVERY component is
            # null; a partially null tuple is itself a local row fault.
            store.db.execute(
                "INSERT INTO local_diags (table_ord, row_id, code, "
                "message, detail, diag_ord) VALUES (?,?,?,?,?,?)",
                (table.ordinal, row_id, "partial_null_reference",
                 f"optional reference {ref.name!r} has some but not all "
                 "components null; it may skip only when every component "
                 "is null",
                 json.dumps({"reference": ref.name,
                             "columns": list(ref.columns),
                             "null_columns": [
                                 col for col, isnull in
                                 zip(ref.columns, null_flags) if isnull]},
                            ensure_ascii=False),
                 _next_diag_ord(store, table, row_id)))
            store.note_write()
            store.db.execute(
                "UPDATE records SET status = 'local_reject' "
                "WHERE table_ord = ? AND row_id = ?",
                (table.ordinal, row_id))
            store.note_write()
            return
        if any_null:
            # Mandatory reference with a null component: mandatory never
            # allows null, so this reference cannot match any parent --
            # key_values only ever stores non-null tuples, hence the edge
            # below is born dead at layer 1.  The null itself stays legal
            # at field level when the column was declared nullable, so no
            # separate local diagnostic is emitted.  Encode nulls with an
            # explicit tag so such tuples remain stable, typed data rather
            # than a concatenated string.
            tagged = [("n", None) if v is None else
                      (("i" if t == "integer" else "s"), v)
                      for v, t in zip(values, types)]
            tuple_text = json.dumps(
                tagged, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False)
        else:
            tuple_text = encode_tuple(values, tuple(types))
        store.db.execute(
            "INSERT INTO ref_edges (table_ord, row_id, ref_name, "
            "target_ord, target_key, key_tuple, is_optional) "
            "VALUES (?,?,?,?,?,?,?)",
            (table.ordinal, row_id, ref.name,
             _table_ordinal(store, ref.target_table), ref.target_key,
             tuple_text, 1 if ref.optional else 0))
        store.note_write()


def _next_diag_ord(store: StagingStore, table: TableContract,
                   row_id: str) -> int:
    row = store.db.execute(
        "SELECT COUNT(*) FROM local_diags WHERE table_ord = ? AND row_id = ?",
        (table.ordinal, row_id)).fetchone()
    return row[0]


def _table_ordinal(store: StagingStore, name: str) -> int:
    # Ordinals are contract order; resolved through a tiny helper table
    # created by the engine so names never become SQL fragments.
    return store.db.execute(
        "SELECT ordinal FROM table_ordinals WHERE name = ?",
        (name,)).fetchone()[0]
