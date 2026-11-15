"""Read-only queries over a delivered admission result.

A reopened process has none of the original input files: every answer is
reconstructed from the delivery directory alone -- the provenance SQLite
database (the same staged records, keys, references and proof edges) and
the sorted JSONL deliverables.  Three questions are supported:

* :func:`explain_row` -- why one identity was kept or rejected;
* :func:`get_proof_node` -- one shared proof node with its complete initial
  candidate set and the proof nodes of those candidates, so an external
  checker can walk the proof DAG (parent layers are always strictly
  smaller, hence acyclic);
* :func:`iter_kept_rows` -- stream the admitted rows of one table.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator, Union

from .config import decode_tuple
from .engine import (CASCADE_REJECT, COMPLETE_MARKER, KEPT, LOCAL_REJECT,
                     REPORT_JSON, STAGING_DB)
from .errors import RelationQueryError


def _connect(out_dir: Path) -> sqlite3.Connection:
    if not (out_dir / COMPLETE_MARKER).is_file():
        raise RelationQueryError(
            f"{out_dir} is not a completed admission result "
            f"({COMPLETE_MARKER} missing); refusing to read an unfinished "
            "or partial delivery")
    db_path = out_dir / STAGING_DB
    if not db_path.is_file():
        raise RelationQueryError(f"{db_path} is missing from the delivery")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    return db


def _resolve_table(db: sqlite3.Connection, table: str) -> int:
    row = db.execute(
        "SELECT ordinal FROM table_ordinals WHERE name = ?",
        (table,)).fetchone()
    if row is None:
        raise RelationQueryError(f"unknown table {table!r}")
    return row[0]


def explain_row(result_dir: Union[str, Path], table: str,
                row_id: str) -> dict:
    """Explain the disposition of one identity from the delivery only."""
    out_dir = Path(result_dir)
    if not isinstance(row_id, str) or not row_id:
        raise RelationQueryError("row_id must be a non-empty string")
    db = _connect(out_dir)
    try:
        table_ord = _resolve_table(db, table)
        row = db.execute(
            "SELECT status, layer, source_file, line_number, payload "
            "FROM records WHERE table_ord = ? AND row_id = ?",
            (table_ord, row_id)).fetchone()
        if row is None:
            raise RelationQueryError(
                f"row_id {row_id!r} is not present in table {table!r}")
        status, layer, source_file, line_number, payload = row
        result: dict = {
            "table": table, "row_id": row_id, "status": status,
            "layer": layer,
            "origin": {"source_file": source_file,
                       "line_number": line_number},
            "record": json.loads(payload),
        }
        if status == LOCAL_REJECT:
            result["local_diagnostics"] = [
                {"code": code, "message": message,
                 "detail": json.loads(detail)}
                for code, message, detail in db.execute(
                    "SELECT code, message, detail FROM local_diags "
                    "WHERE table_ord = ? AND row_id = ? ORDER BY diag_ord",
                    (table_ord, row_id))]
            # Local rows deliberately carry no reference-failure proof.
            return result
        if status == CASCADE_REJECT:
            proof_id = db.execute(
                "SELECT proof_id FROM row_proofs WHERE table_ord = ? "
                "AND row_id = ?", (table_ord, row_id)).fetchone()[0]
            result["primary_proof"] = _load_proof(db, proof_id)
            result["all_failing_references"] = _all_failing(db, table_ord,
                                                            row_id)
            return result
        # KEPT: one surviving parent witness per matched reference, plus
        # explicit markers for all-null optional references.
        result["witnesses"] = [
            {"reference": ref_name,
             "parent_table": _table_name(db, parent_ord),
             "parent_row_id": parent_row_id}
            for ref_name, parent_ord, parent_row_id in db.execute(
                "SELECT ref_name, parent_ord, parent_row_id FROM witnesses "
                "WHERE table_ord = ? AND row_id = ? "
                "ORDER BY CAST(ref_name AS BLOB)", (table_ord, row_id))]
        result["all_null_optional_references"] = [
            ref_name for (ref_name,) in db.execute(
                "SELECT ref_name FROM ref_skips WHERE table_ord = ? "
                "AND row_id = ? ORDER BY CAST(ref_name AS BLOB)",
                (table_ord, row_id))]
        return result
    finally:
        db.close()


def _all_failing(db: sqlite3.Connection, table_ord: int,
                 row_id: str) -> list[dict]:
    out = []
    for ref_name, proof_id, layer in db.execute(
            """
            SELECT e.ref_name, p.proof_id, e.dead_round
            FROM ref_edges e
            JOIN edge_groups g
              ON g.table_ord = e.table_ord AND g.ref_name = e.ref_name
             AND g.target_ord = e.target_ord
             AND g.target_key = e.target_key
             AND g.key_tuple = e.key_tuple
            JOIN proofs p ON p.group_id = g.group_id
            WHERE e.table_ord = ? AND e.row_id = ? AND e.live = 0
            ORDER BY e.dead_round, CAST(e.ref_name AS BLOB)
            """, (table_ord, row_id)):
        out.append({"reference": ref_name, "layer": layer,
                    "proof_id": proof_id})
    return out


def get_proof_node(result_dir: Union[str, Path], proof_id: int) -> dict:
    """Return one shared proof node and its candidate proof references."""
    out_dir = Path(result_dir)
    db = _connect(out_dir)
    try:
        if not isinstance(proof_id, int) or isinstance(proof_id, bool):
            raise RelationQueryError("proof_id must be an integer")
        return _load_proof(db, proof_id)
    finally:
        db.close()


def _load_proof(db: sqlite3.Connection, proof_id: int) -> dict:
    header = db.execute(
        """
        SELECT p.reject_layer, g.table_ord, g.ref_name, g.target_ord,
               g.target_key, g.key_tuple
        FROM proofs p JOIN edge_groups g ON g.group_id = p.group_id
        WHERE p.proof_id = ?
        """, (proof_id,)).fetchone()
    if header is None:
        raise RelationQueryError(f"unknown proof_id {proof_id}")
    layer, table_ord, ref_name, target_ord, target_key, key_tuple = header
    candidates = []
    for parent_ord, parent_row_id, parent_layer, cand_ord in db.execute(
            "SELECT parent_ord, parent_row_id, parent_layer, cand_ord "
            "FROM proof_candidates WHERE proof_id = ? ORDER BY cand_ord",
            (proof_id,)):
        # Each candidate points at its own proof node when it was itself
        # rejected; locally rejected rows carry layer 0 and no node.
        parent_proof = db.execute(
            "SELECT rp.proof_id FROM row_proofs rp "
            "JOIN records r ON r.table_ord = rp.table_ord "
            "AND r.row_id = rp.row_id "
            "WHERE rp.table_ord = ? AND rp.row_id = ?",
            (parent_ord, parent_row_id)).fetchone()
        candidates.append({
            "parent_table": _table_name(db, parent_ord),
            "parent_row_id": parent_row_id,
            "parent_layer": parent_layer,
            "parent_proof_id": parent_proof[0] if parent_proof else None,
            "candidate_ordinal": cand_ord})
    return {
        "proof_id": proof_id,
        "layer": layer,
        "child_table": _table_name(db, table_ord),
        "reference": ref_name,
        "target_table": _table_name(db, target_ord),
        "target_key": target_key,
        "key_tuple": decode_tuple(key_tuple),
        # An empty list is the explicit "no initial parent candidate" set.
        "initial_candidates": candidates,
        "candidate_count": len(candidates)}


def _table_name(db: sqlite3.Connection, ordinal: int) -> str:
    return db.execute(
        "SELECT name FROM table_ordinals WHERE ordinal = ?",
        (ordinal,)).fetchone()[0]


def iter_kept_rows(result_dir: Union[str, Path], table: str) -> Iterator[dict]:
    """Yield the admitted rows of one table in row_id UTF-8 byte order."""
    out_dir = Path(result_dir)
    db = _connect(out_dir)
    try:
        table_ord = _resolve_table(db, table)
        cursor = db.execute(
            "SELECT payload FROM records WHERE table_ord = ? AND status = ? "
            "ORDER BY CAST(row_id AS BLOB)", (table_ord, KEPT))
        for (payload_text,) in cursor:
            yield json.loads(payload_text)
    finally:
        db.close()


def table_deliverable_path(result_dir: Union[str, Path], table: str,
                           *, status: str = KEPT) -> Path:
    """Filesystem path of one table's JSONL deliverable inside the result."""
    out_dir = Path(result_dir)
    db = _connect(out_dir)
    try:
        table_ord = _resolve_table(db, table)
        name = _table_name(db, table_ord)
    finally:
        db.close()
    if status not in (KEPT, LOCAL_REJECT, CASCADE_REJECT):
        raise RelationQueryError(f"unknown deliverable status {status!r}")
    report = json.loads((out_dir / REPORT_JSON).read_text(encoding="utf-8"))
    for meta in report["tables"]:
        if meta["name"] == name:
            return out_dir / meta["deliverables"][status]
    raise RelationQueryError(f"table {table!r} absent from report")  # pragma
