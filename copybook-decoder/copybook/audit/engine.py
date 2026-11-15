"""Cross-record handoff validation engine.

The engine walks the merged physical-line stream once, maintaining only:

* the single current control-record state,
* the fixed category table,
* per-category accumulators for the *open* batch only,
* one SQLite database on disk for every detail payload, batch result and
  diagnostic.

It therefore never holds a whole batch -- let alone a whole file -- of
details in memory.  SQLite is used strictly as disk staging and as the
store that proves whether a batch id has already appeared.

Structural faults that cannot be contained in one batch (a BHEAD while a
batch is still open, a detail or quarantine line outside any batch, an
unknown record type, missing headers/tails, duplicate batch ids, file id
mismatch, lines after FTAIL, wrong FTAIL totals) reject the *whole file*:
the engine never scans forward looking for a "recovery" header.  Batch-
local faults (bad seq, wrong batch id, illegal category/quantity, an
in-batch quarantine line) reject only the current batch, whose BTAIL is
still read so later batches can be checked independently.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Optional, Union

from .config import (AuditConfig, FieldPath, format_milli, parse_count,
                     parse_quantity_milli)
from .errors import AuditInputError
from .evidence import Evidence, iter_evidence

RECORD_TYPES = ("FHEAD", "BHEAD", "DETAIL", "BTAIL", "FTAIL")

# Final conclusions.
PASS = "pass"                    # structure valid, every batch accepted
BUSINESS_REJECTED = "business_rejected"   # audited; some or all details refused
UNVERIFIABLE = "unverifiable"    # input/config evidence incomplete

# Batch / check statuses.
ACCEPTED = "accepted"
REJECTED = "rejected"
CHECK_OK = "ok"
CHECK_MISMATCH = "mismatch"
CHECK_UNDECIDABLE = "undecidable"
CHECK_INVALID = "invalid_declared"

# Max rows staged inside one SQLite transaction before a flush. This is the
# only write-buffer that scales with input, and it is bounded explicitly.
COMMIT_BATCH = 2000


@dataclass
class _OpenBatch:
    ordinal: int
    batch_id: str
    bhead_line: int
    bhead_start: int
    bhead_end: int
    expected_seq: int = 1
    detail_positions: int = 0          # DETAIL + quarantine lines in batch
    quarantine_count: int = 0          # counters stay in memory; actual line
    actual_milli: dict[str, int] = dc_field(default_factory=dict)
    has_diagnostic: bool = False


def _diag(code: str, message: str, ev: Optional[Evidence] = None,
          *, line_number: Optional[int] = None,
          byte_start: Optional[int] = None,
          byte_end: Optional[int] = None,
          detail: Optional[dict] = None) -> dict:
    if ev is not None:
        line_number = ev.line_number
        byte_start, byte_end = ev.byte_start, ev.byte_end
    return {
        "code": code,
        "message": message,
        "line_number": line_number,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "detail": detail or {},
    }


class AuditEngine:
    """State machine over one merged evidence stream."""

    def __init__(self, config: AuditConfig, conn: sqlite3.Connection):
        self.cfg = config
        self.db = conn
        # Disk staging only: WAL keeps readers/writers off the main file
        # lock; NORMAL sync avoids an fsync per statement while staying
        # crash-safe across explicit commits.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self._create_tables()
        self._create_indexes()
        self._uncommitted = 0

        self.phase = "head"              # head | body | done
        self.file_id_head: Optional[str] = None
        self.file_id_tail: Optional[str] = None
        self.fhead_line: Optional[int] = None
        self.ftail_line: Optional[int] = None
        self.fhead_range: Optional[tuple[int, int]] = None
        self.ftail_range: Optional[tuple[int, int]] = None
        self.ftail_batch_count_text: Optional[str] = None
        self.ftail_detail_count_text: Optional[str] = None
        self.bhead_seen = 0
        self.batch_detail_positions = 0
        self.open: Optional[_OpenBatch] = None
        self.file_rejected = False

    def _checkpoint(self) -> None:
        """Commit a bounded batch of writes; staging stays on disk."""
        self.db.commit()
        self._uncommitted = 0

    def _note_write(self) -> None:
        # Flush every COMMIT_BATCH rows so the uncommitted window never
        # grows with file size -- memory stays independent of detail count.
        self._uncommitted += 1
        if self._uncommitted >= COMMIT_BATCH:
            self._checkpoint()

    # ----------------------------------------------------------- schema

    def _create_tables(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE seen_batches (
                batch_id TEXT PRIMARY KEY,
                bhead_line INTEGER NOT NULL
            );
            CREATE TABLE batch_meta (
                ordinal INTEGER PRIMARY KEY,
                batch_id TEXT NOT NULL,
                bhead_line INTEGER NOT NULL,
                bhead_byte_start INTEGER NOT NULL,
                bhead_byte_end INTEGER NOT NULL,
                btail_line INTEGER,
                status TEXT,
                declared_detail_count TEXT,
                actual_detail_count INTEGER,
                detail_count_ok INTEGER,
                quantity_check_status TEXT,
                quantity_reason TEXT,
                has_diagnostic INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE batch_totals (
                ordinal INTEGER NOT NULL,
                category TEXT NOT NULL,
                declared_text TEXT,
                declared_milli INTEGER,
                actual_milli INTEGER,
                undecidable INTEGER NOT NULL,
                ok INTEGER,
                reason TEXT,
                PRIMARY KEY (ordinal, category)
            );
            CREATE TABLE details (
                line_number INTEGER PRIMARY KEY,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                batch_id TEXT NOT NULL,
                seq INTEGER,
                category TEXT,
                quantity_milli INTEGER,
                payload TEXT NOT NULL
            );
            CREATE TABLE quarantine_lines (
                line_number INTEGER PRIMARY KEY,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                ordinal INTEGER,
                reason TEXT NOT NULL
            );
            CREATE TABLE batch_diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ordinal INTEGER,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                line_number INTEGER,
                byte_start INTEGER,
                byte_end INTEGER,
                detail TEXT NOT NULL
            );
            CREATE TABLE file_diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                line_number INTEGER,
                byte_start INTEGER,
                byte_end INTEGER,
                detail TEXT NOT NULL
            );
            """)

    def _create_indexes(self) -> None:
        # Secondary indexes for the bounded report/delivery queries.
        self.db.executescript(
            """
            CREATE INDEX idx_details_ordinal ON details(ordinal);
            CREATE INDEX idx_diag_ordinal ON batch_diagnostics(ordinal);
            CREATE INDEX idx_quar_ordinal ON quarantine_lines(ordinal);
            """)

    # ----------------------------------------------------- diagnostic I/O

    def _file_error(self, diag: dict) -> None:
        self.file_rejected = True
        self.db.execute(
            "INSERT INTO file_diagnostics"
            "(code, message, line_number, byte_start, byte_end, detail)"
            " VALUES (?,?,?,?,?,?)",
            (diag["code"], diag["message"], diag["line_number"],
             diag["byte_start"], diag["byte_end"],
             json.dumps(diag["detail"], ensure_ascii=False)))
        self._note_write()

    def _batch_error(self, diag: dict) -> None:
        ordinal = self.open.ordinal if self.open is not None else None
        if self.open is not None:
            self.open.has_diagnostic = True
        self.db.execute(
            "INSERT INTO batch_diagnostics"
            "(ordinal, code, message, line_number, byte_start, byte_end, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (ordinal, diag["code"], diag["message"], diag["line_number"],
             diag["byte_start"], diag["byte_end"],
             json.dumps(diag["detail"], ensure_ascii=False)))
        self._note_write()

    # ------------------------------------------------------------ helpers

    def _text(self, path: FieldPath, ev: Evidence) -> Optional[str]:
        return path.as_text(ev.fields)

    def _record_type(self, ev: Evidence) -> Optional[str]:
        value = self._text(self.cfg.record_type_path, ev)
        if value is None:
            return None
        value = str(value)
        return value if value in RECORD_TYPES else value

    # ------------------------------------------------------------- main

    def run(self, stream) -> None:
        for ev in stream:
            if self.phase == "head":
                self._handle_head_phase(ev)
            elif self.phase == "body":
                self._handle_body_phase(ev)
            else:
                self._file_error(_diag(
                    "data_after_ftail",
                    "no evidence is allowed after the unique FTAIL record",
                    ev,
                    detail={"kind": ev.kind}))

    def _handle_head_phase(self, ev: Evidence) -> None:
        rtype = self._record_type(ev) if ev.kind == "record" else None
        if rtype == "FHEAD":
            if self.fhead_line is not None:
                self._file_error(_diag(
                    "duplicate_fhead",
                    "the file must contain exactly one FHEAD", ev))
            else:
                self.fhead_line = ev.line_number
                self.fhead_range = (ev.byte_start, ev.byte_end)
                file_id = self._text(self.cfg.file_id_path, ev)
                self.file_id_head = file_id
                if not file_id:
                    self._file_error(_diag(
                        "missing_file_id",
                        "FHEAD does not carry a non-empty file_id", ev))
            self.phase = "body"
            return
        # Anything before the first FHEAD -- quarantine line included.
        self._file_error(_diag(
            "unexpected_before_fhead",
            "every physical line before FHEAD is illegal; expected a single "
            "FHEAD first", ev,
            detail={"kind": ev.kind, "record_type": rtype}))

    def _handle_body_phase(self, ev: Evidence) -> None:
        if ev.kind == "quarantine":
            self._handle_quarantine(ev)
            return

        rtype = self._record_type(ev)
        if rtype == "FHEAD":
            self._file_error(_diag(
                "duplicate_fhead",
                "the file must contain exactly one FHEAD", ev))
            return
        if rtype == "FTAIL":
            self._handle_ftail(ev)
            return
        if rtype == "BHEAD":
            self._handle_bhead(ev)
            return
        if rtype == "BTAIL":
            if self.open is None:
                self._file_error(_diag(
                    "btail_without_bhead",
                    "BTAIL has no open BHEAD batch", ev))
                return
            self._close_batch(ev)
            return
        if rtype == "DETAIL":
            if self.open is None:
                self._file_error(_diag(
                    "detail_outside_batch",
                    "DETAIL record appears outside any BHEAD..BTAIL batch",
                    ev))
                return
            self._handle_detail(ev)
            return
        self._file_error(_diag(
            "unknown_record_type",
            f"record type {rtype!r} is not one of "
            f"{', '.join(RECORD_TYPES)}", ev,
            detail={"record_type": rtype}))

    # ------------------------------------------------------------ BHEAD

    def _handle_bhead(self, ev: Evidence) -> None:
        if self.open is not None:
            # File-level fault: the previous batch never got its BTAIL.
            self._file_error(_diag(
                "unterminated_batch",
                f"BHEAD at line {ev.line_number} starts a new batch while "
                f"batch {self.open.batch_id!r} (from line "
                f"{self.open.bhead_line}) has no BTAIL", ev,
                detail={"previous_batch_id": self.open.batch_id,
                        "previous_bhead_line": self.open.bhead_line}))
            # Finalize the orphan batch as rejected, then honour this BHEAD
            # so BHEAD counting still reflects what was physically seen.
            self._abandon_open_batch()

        batch_id = self._text(self.cfg.batch_id_path, ev)
        self.bhead_seen += 1
        ordinal = self.bhead_seen
        if not batch_id:
            self._file_error(_diag(
                "missing_batch_id",
                "BHEAD does not carry a non-empty batch_id", ev))
            batch_id = batch_id or ""
        else:
            row = self.db.execute(
                "SELECT bhead_line FROM seen_batches WHERE batch_id = ?",
                (batch_id,)).fetchone()
            if row is not None:
                self._file_error(_diag(
                    "duplicate_batch_id",
                    f"batch_id {batch_id!r} already appeared at line "
                    f"{row[0]}; batch ids must be unique within the file",
                    ev,
                    detail={"previous_bhead_line": row[0]}))
            else:
                self.db.execute(
                    "INSERT INTO seen_batches (batch_id, bhead_line)"
                    " VALUES (?, ?)", (batch_id, ev.line_number))

        self.db.execute(
            "INSERT INTO batch_meta"
            "(ordinal, batch_id, bhead_line, bhead_byte_start, bhead_byte_end)"
            " VALUES (?,?,?,?,?)",
            (ordinal, batch_id, ev.line_number, ev.byte_start, ev.byte_end))
        self._note_write()
        self.open = _OpenBatch(
            ordinal=ordinal, batch_id=batch_id,
            bhead_line=ev.line_number,
            bhead_start=ev.byte_start, bhead_end=ev.byte_end)
        self.open.actual_milli = {cat: 0 for cat in self.cfg.categories}

    # ----------------------------------------------------------- DETAIL

    def _handle_detail(self, ev: Evidence) -> None:
        batch = self.open
        assert batch is not None
        batch.detail_positions += 1
        self.batch_detail_positions += 1

        # seq must restart at 1 in every batch and step by one across the
        # readable DETAIL records; quarantined lines have no readable seq and
        # do not advance this expectation (they still count as positions).
        seq_text = self._text(self.cfg.seq_path, ev)
        seq_value = parse_count(seq_text)
        if seq_value != batch.expected_seq:
            self._batch_error(_diag(
                "bad_seq",
                f"DETAIL seq {seq_text!r} does not match expected "
                f"{batch.expected_seq}", ev,
                detail={"expected": batch.expected_seq, "actual": seq_text}))

        detail_batch_id = self._text(self.cfg.batch_id_path, ev)
        if detail_batch_id != batch.batch_id:
            self._batch_error(_diag(
                "batch_id_mismatch",
                f"DETAIL points at batch_id {detail_batch_id!r} but the open "
                f"batch is {batch.batch_id!r}", ev,
                detail={"expected": batch.batch_id,
                        "actual": detail_batch_id}))

        category = self._text(self.cfg.category_path, ev)
        known_category = category in self.cfg.categories
        if not known_category:
            self._batch_error(_diag(
                "invalid_category",
                f"DETAIL category {category!r} is not in the fixed scheme "
                f"{list(self.cfg.categories)}", ev,
                detail={"actual": category}))

        qty_text = self._text(self.cfg.quantity_path, ev)
        qty_milli = parse_quantity_milli(qty_text)
        if qty_milli is None:
            self._batch_error(_diag(
                "invalid_quantity",
                f"DETAIL quantity {qty_text!r} is not a signed decimal with "
                "at most three fractional digits", ev,
                detail={"actual": qty_text}))
        elif known_category:
            batch.actual_milli[category] += qty_milli

        self.db.execute(
            "INSERT INTO details"
            "(line_number, byte_start, byte_end, ordinal, batch_id, seq, "
            " category, quantity_milli, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ev.line_number, ev.byte_start, ev.byte_end, batch.ordinal,
             batch.batch_id, seq_value, category, qty_milli,
             json.dumps(ev.raw, ensure_ascii=False)))
        self._note_write()
        batch.expected_seq += 1

    def _handle_quarantine(self, ev: Evidence) -> None:
        if self.open is None:
            # Stage the position (ordinal NULL) so the report lists every
            # quarantined line, then raise the non-containable file fault.
            self.db.execute(
                "INSERT INTO quarantine_lines"
                "(line_number, byte_start, byte_end, ordinal, reason)"
                " VALUES (?,?,?,?,?)",
                (ev.line_number, ev.byte_start, ev.byte_end, None,
                 str(ev.raw.get("reason", "unknown"))))
            self._note_write()
            self._file_error(_diag(
                "orphan_quarantine",
                "quarantined physical line appears outside any batch; its "
                "business values can never be reconciled", ev,
                detail={"quarantine_reason":
                        ev.raw.get("reason", "unknown")}))
            return
        batch = self.open
        batch.detail_positions += 1
        self.batch_detail_positions += 1
        batch.quarantine_count += 1
        # The physical position is staged on disk; no per-batch line list
        # is ever held in memory.
        self.db.execute(
            "INSERT INTO quarantine_lines"
            "(line_number, byte_start, byte_end, ordinal, reason)"
            " VALUES (?,?,?,?,?)",
            (ev.line_number, ev.byte_start, ev.byte_end, batch.ordinal,
             str(ev.raw.get("reason", "unknown"))))
        self._note_write()
        # A received detail position whose values are unknown: the batch is
        # refused, and quantity reconciliation becomes undecidable. It still
        # counts as a physical position for BTAIL/FTAIL row counts, but its
        # seq is unknowable, so it does not fabricate a seq expectation for
        # the DETAIL records that follow -- seq continuity is checked only
        # across readable DETAIL lines.
        self._batch_error(_diag(
            "quarantined_detail",
            "batch contains a quarantined physical line; the detail position "
            "was received but its business values are unknown", ev,
            detail={"quarantine_reason": ev.raw.get("reason", "unknown")}))

    # ------------------------------------------------------------- BTAIL

    def _close_batch(self, btail_ev: Evidence) -> None:
        batch = self.open
        assert batch is not None
        declared_count_text = self._text(
            self.cfg.btail_detail_count_path, btail_ev)
        declared_count = parse_count(declared_count_text)
        count_ok = declared_count == batch.detail_positions
        if declared_count is None:
            self._batch_error(_diag(
                "invalid_btail_count",
                f"BTAIL detail count {declared_count_text!r} is not a "
                "non-negative integer", btail_ev,
                detail={"actual": declared_count_text}))
        elif not count_ok:
            self._batch_error(_diag(
                "btail_count_mismatch",
                f"BTAIL declares {declared_count} detail line(s) but the "
                f"batch physically contains {batch.detail_positions}",
                btail_ev,
                detail={"declared": declared_count,
                        "actual": batch.detail_positions,
                        "quarantine_line_count": batch.quarantine_count}))

        undecidable = batch.quarantine_count > 0
        undecidable_reason = None
        overall_qty_status = CHECK_OK
        if undecidable:
            undecidable_reason = (
                f"{batch.quarantine_count} quarantined detail position(s) "
                "hide their category and quantity; category totals cannot be "
                "decided (not treated as zero). Their physical line numbers "
                "are listed in this batch's quarantined_detail diagnostics")
            overall_qty_status = CHECK_UNDECIDABLE

        any_total_mismatch = False
        any_invalid_declared = False
        for cat in self.cfg.categories:
            declared_text = self._text(
                self.cfg.btail_total_paths[cat], btail_ev)
            declared_milli = parse_quantity_milli(declared_text)
            actual_milli = batch.actual_milli[cat]
            if undecidable:
                ok = None
                reason = undecidable_reason
            elif declared_milli is None:
                ok = False
                reason = (f"declared total {declared_text!r} is not a legal "
                          "signed decimal with up to three fraction digits")
                any_invalid_declared = True
                self._batch_error(_diag(
                    "invalid_btail_total",
                    f"BTAIL total for category {cat} is unparseable",
                    btail_ev,
                    detail={"category": cat, "actual": declared_text}))
            else:
                ok = declared_milli == actual_milli
                reason = None
                if not ok:
                    any_total_mismatch = True
                    self._batch_error(_diag(
                        "btail_total_mismatch",
                        f"BTAIL total for category {cat} declares "
                        f"{format_milli(declared_milli)} but the received "
                        f"details sum to {format_milli(actual_milli)}",
                        btail_ev,
                        detail={"category": cat,
                                "declared_milli": declared_milli,
                                "actual_milli": actual_milli}))
            self.db.execute(
                "INSERT INTO batch_totals"
                "(ordinal, category, declared_text, declared_milli, "
                " actual_milli, undecidable, ok, reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch.ordinal, cat, declared_text, declared_milli,
                 actual_milli, 1 if undecidable else 0,
                 None if ok is None else int(ok), reason))
        self._note_write()

        if not undecidable:
            if any_invalid_declared:
                overall_qty_status = CHECK_INVALID
            elif any_total_mismatch:
                overall_qty_status = CHECK_MISMATCH

        status = ACCEPTED if (
            not batch.has_diagnostic and count_ok
            and overall_qty_status == CHECK_OK
        ) else REJECTED

        self.db.execute(
            "UPDATE batch_meta SET btail_line = ?, status = ?, "
            "declared_detail_count = ?, actual_detail_count = ?, "
            "detail_count_ok = ?, quantity_check_status = ?, "
            "quantity_reason = ?, has_diagnostic = ? WHERE ordinal = ?",
            (btail_ev.line_number, status, declared_count_text,
             batch.detail_positions, int(count_ok), overall_qty_status,
             undecidable_reason, int(batch.has_diagnostic), batch.ordinal))
        self._note_write()
        self.open = None

    def _abandon_open_batch(self) -> None:
        """Close an open batch that never received a BTAIL (file-level fault)."""
        batch = self.open
        assert batch is not None
        self._batch_error(_diag(
            "missing_btail",
            f"batch {batch.batch_id!r} opened at line {batch.bhead_line} "
            "ends without a BTAIL",
            line_number=batch.bhead_line,
            byte_start=batch.bhead_start, byte_end=batch.bhead_end))
        for cat in self.cfg.categories:
            if batch.quarantine_count:
                reason = (
                    "batch has no BTAIL and "
                    f"{batch.quarantine_count} quarantined detail "
                    "position(s) also hide their values; totals cannot be "
                    "decided")
            else:
                reason = ("batch has no BTAIL; declared control totals "
                          "are absent")
            self.db.execute(
                "INSERT INTO batch_totals"
                "(ordinal, category, declared_text, declared_milli, "
                " actual_milli, undecidable, ok, reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch.ordinal, cat, None, None,
                 batch.actual_milli[cat],
                 1 if batch.quarantine_count else 0, None, reason))
        self.db.execute(
            "UPDATE batch_meta SET btail_line = NULL, status = ?, "
            "declared_detail_count = NULL, actual_detail_count = ?, "
            "detail_count_ok = NULL, quantity_check_status = ?, "
            "quantity_reason = ?, has_diagnostic = 1 WHERE ordinal = ?",
            (REJECTED, batch.detail_positions, CHECK_UNDECIDABLE,
             "no BTAIL received", batch.ordinal))
        self._note_write()
        self.open = None

    # ------------------------------------------------------------- FTAIL

    def _handle_ftail(self, ev: Evidence) -> None:
        if self.open is not None:
            self._file_error(_diag(
                "missing_btail",
                f"FTAIL arrives while batch {self.open.batch_id!r} (opened at "
                f"line {self.open.bhead_line}) has no BTAIL", ev))
            self._abandon_open_batch()

        self.ftail_line = ev.line_number
        self.ftail_range = (ev.byte_start, ev.byte_end)
        self.file_id_tail = self._text(self.cfg.file_id_path, ev)
        if not self.file_id_tail:
            self._file_error(_diag(
                "missing_file_id",
                "FTAIL does not carry a non-empty file_id", ev))
        if self.file_id_head != self.file_id_tail:
            self._file_error(_diag(
                "file_id_mismatch",
                f"FHEAD file_id {self.file_id_head!r} differs from FTAIL "
                f"file_id {self.file_id_tail!r}", ev,
                detail={"fhead": self.file_id_head,
                        "ftail": self.file_id_tail}))

        declared_batches_text = self._text(
            self.cfg.ftail_batch_count_path, ev)
        self.ftail_batch_count_text = declared_batches_text
        declared_batches = parse_count(declared_batches_text)
        if declared_batches is None:
            self._file_error(_diag(
                "invalid_ftail_batch_count",
                f"FTAIL batch_count {declared_batches_text!r} is not a "
                "non-negative integer", ev,
                detail={"actual": declared_batches_text}))
        elif declared_batches != self.bhead_seen:
            self._file_error(_diag(
                "ftail_batch_count_mismatch",
                f"FTAIL declares {declared_batches} batch(es) but "
                f"{self.bhead_seen} BHEAD record(s) were seen", ev,
                detail={"declared": declared_batches,
                        "actual": self.bhead_seen}))

        declared_details_text = self._text(
            self.cfg.ftail_detail_count_path, ev)
        self.ftail_detail_count_text = declared_details_text
        declared_details = parse_count(declared_details_text)
        if declared_details is None:
            self._file_error(_diag(
                "invalid_ftail_detail_count",
                f"FTAIL detail_count {declared_details_text!r} is not a "
                "non-negative integer", ev,
                detail={"actual": declared_details_text}))
        elif declared_details != self.batch_detail_positions:
            self._file_error(_diag(
                "ftail_detail_count_mismatch",
                f"FTAIL declares {declared_details} in-batch detail "
                f"position(s) but {self.batch_detail_positions} were seen "
                "(DETAIL records plus in-batch quarantine lines, rejected "
                "batches included)", ev,
                detail={"declared": declared_details,
                        "actual": self.batch_detail_positions}))
        self.phase = "done"

    # --------------------------------------------------------------- EOF

    def finish(self) -> None:
        if self.phase == "head":
            self._file_error(_diag(
                "missing_fhead",
                "the evidence stream contains no FHEAD record",
                line_number=None))
        if self.open is not None:
            open_batch = self.open
            self._file_error(_diag(
                "missing_btail",
                f"batch {open_batch.batch_id!r} opened at line "
                f"{open_batch.bhead_line} reaches end of file without BTAIL",
                line_number=open_batch.bhead_line,
                byte_start=open_batch.bhead_start,
                byte_end=open_batch.bhead_end))
            self._abandon_open_batch()
        if self.phase != "done":
            self._file_error(_diag(
                "missing_ftail",
                "the evidence stream ends without the unique FTAIL record",
                line_number=None))
        # Flush the final bounded write window before the report phase.
        self._checkpoint()
        # Check the WAL back into the main staging file so staging size is
        # reported as a single database footprint.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.commit()


# ---------------------------------------------------------------- report

def _peak_rss_kb() -> Optional[int]:
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX platforms
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


# Diagnostics copied into the in-memory Python result are capped; the full,
# unbounded set always lands in audit_report.json and the SQLite staging.
SUMMARY_DIAG_CAP = 20


def _diag_row(row: tuple) -> dict:
    code, message, line_number, byte_start, byte_end, detail_text = row
    try:
        detail = json.loads(detail_text)
    except (TypeError, json.JSONDecodeError):
        detail = {}
    return {
        "code": code,
        "message": message,
        "line_number": line_number,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "detail": detail,
    }


def _batch_summary(conn: sqlite3.Connection, engine: "AuditEngine",
                   ordinal: int) -> dict:
    """One batch's report section, read with bounded per-batch queries."""
    meta = conn.execute(
        "SELECT batch_id, bhead_line, btail_line, status, "
        "declared_detail_count, actual_detail_count, detail_count_ok, "
        "quantity_check_status, quantity_reason "
        "FROM batch_meta WHERE ordinal = ?", (ordinal,)).fetchone()
    (batch_id, bhead_line, btail_line, status, declared_count,
     actual_count, count_ok, qty_status, qty_reason) = meta

    totals = []
    for trow in conn.execute(
            "SELECT category, declared_text, declared_milli, actual_milli, "
            "undecidable, ok, reason FROM batch_totals "
            "WHERE ordinal = ? ORDER BY category", (ordinal,)):
        cat, declared_text, declared_milli, actual_milli, undec, ok, reason = trow
        totals.append({
            "category": cat,
            "declared": declared_text,
            "declared_milli": declared_milli,
            "actual": None if undec else format_milli(actual_milli),
            "actual_milli": None if undec else actual_milli,
            "ok": ok,
            "undecidable": bool(undec),
            "undecidable_reason": reason if undec else None,
        })

    detail_count_total = conn.execute(
        "SELECT COUNT(*) FROM details WHERE ordinal = ?",
        (ordinal,)).fetchone()[0]
    quarantine_count = conn.execute(
        "SELECT COUNT(*) FROM quarantine_lines WHERE ordinal = ?",
        (ordinal,)).fetchone()[0]
    # Per-batch diagnostic count is bounded by the number of control
    # records in the batch for structural faults; cap the copied list.
    diag_cursor = conn.execute(
        "SELECT code, message, line_number, byte_start, byte_end, detail "
        "FROM batch_diagnostics WHERE ordinal = ? ORDER BY id "
        "LIMIT ?", (ordinal, SUMMARY_DIAG_CAP))
    diagnostics = [_diag_row(row) for row in diag_cursor]
    diag_total = conn.execute(
        "SELECT COUNT(*) FROM batch_diagnostics WHERE ordinal = ?",
        (ordinal,)).fetchone()[0]

    return {
        "ordinal": ordinal,
        "batch_id": batch_id,
        "status": status,
        "bhead_line": bhead_line,
        "btail_line": btail_line,
        "detail_count": {
            "declared": declared_count,
            "actual": actual_count,
            "ok": bool(count_ok) if count_ok is not None else None,
        },
        "quantity_check": {
            "status": qty_status,
            "undecidable_reason": qty_reason,
        },
        "category_totals": totals,
        "received_detail_positions": detail_count_total + quarantine_count,
        "quarantine_line_count": quarantine_count,
        "diagnostic_count": diag_total,
        "diagnostics": diagnostics,
        "diagnostics_truncated": diag_total > SUMMARY_DIAG_CAP,
    }


def _stream_accepted_details(path: Path, conn: sqlite3.Connection,
                             engine: "AuditEngine") -> int:
    """Write deliverable details one row at a time; return the row count."""
    if engine.file_rejected:
        path.write_text("", encoding="utf-8")
        return 0
    cursor = conn.execute(
        "SELECT d.payload, d.batch_id "
        "FROM details d JOIN batch_meta b ON b.ordinal = d.ordinal "
        "WHERE b.status = ? ORDER BY d.line_number", (ACCEPTED,))
    total = 0
    with path.open("w", encoding="utf-8") as fh:
        for payload_text, batch_id in cursor:
            record = json.loads(payload_text)
            # Preserve original meta/fields verbatim; only annotate.
            record["file_id"] = engine.file_id_head
            record["batch_id"] = batch_id
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            total += 1
    return total


def _write_report(path: Path, conn: sqlite3.Connection,
                  engine: "AuditEngine", config: AuditConfig,
                  *, expected_source: str, expected_lines: int,
                  accepted_detail_count: int, staging_bytes: int,
                  output_dir: Path, batch_summaries: list[dict],
                  conclusion: str) -> None:
    """Emit audit_report.json by streaming long arrays from SQLite."""
    file_diag_total = conn.execute(
        "SELECT COUNT(*) FROM file_diagnostics").fetchone()[0]

    file_checks = {
        "fhead_line": engine.fhead_line,
        "ftail_line": engine.ftail_line,
        "file_id": {
            "fhead": engine.file_id_head,
            "ftail": engine.file_id_tail,
            "ok": (engine.file_id_head is not None
                   and engine.file_id_head == engine.file_id_tail),
        },
        "batch_count": {
            "declared": engine.ftail_batch_count_text,
            "actual": engine.bhead_seen,
            "ok": (parse_count(engine.ftail_batch_count_text)
                   == engine.bhead_seen
                   if engine.ftail_line is not None else False),
        },
        "detail_count": {
            "declared": engine.ftail_detail_count_text,
            "actual": engine.batch_detail_positions,
            "ok": (parse_count(engine.ftail_detail_count_text)
                   == engine.batch_detail_positions
                   if engine.ftail_line is not None else False),
        },
    }

    def write_json_array(fh, sql, params, key, mapper):
        fh.write(f'  "{key}": [')
        first = True
        for row in conn.execute(sql, params):
            if not first:
                fh.write(",")
            fh.write("\n    " + json.dumps(mapper(row), ensure_ascii=False))
            first = False
        if not first:
            fh.write("\n  ")
        fh.write("],\n")

    with path.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        fh.write(f'  "conclusion": {json.dumps(conclusion)},\n')
        fh.write(f'  "expected_source": {json.dumps(expected_source)},\n')
        fh.write(f'  "expected_lines": {expected_lines},\n')
        fh.write(f'  "file_rejected": {str(engine.file_rejected).lower()},\n')
        fh.write('  "file_checks": '
                 + json.dumps(file_checks, ensure_ascii=False) + ",\n")

        # batches: number of batches is bounded by the control-record count,
        # but each section is still emitted individually.
        fh.write('  "batches": [')
        for i, summary in enumerate(batch_summaries):
            if i:
                fh.write(",")
            fh.write("\n    " + json.dumps(summary, ensure_ascii=False))
        if batch_summaries:
            fh.write("\n  ")
        fh.write("],\n")

        # accepted line numbers may be huge: stream them from disk. A
        # file-level rejection revokes every batch's acceptance, so this
        # array is empty in that case even if some batches looked good.
        if engine.file_rejected:
            fh.write('  "accepted_detail_line_numbers": [],\n')
        else:
            write_json_array(
                fh,
                "SELECT d.line_number FROM details d JOIN batch_meta b "
                "ON b.ordinal = d.ordinal WHERE b.status = ? "
                "ORDER BY d.line_number",
                (ACCEPTED,), "accepted_detail_line_numbers",
                lambda row: row[0])

        # quarantine positions, in source order, with their byte ranges.
        write_json_array(
            fh,
            "SELECT line_number, byte_start, byte_end, ordinal, reason "
            "FROM quarantine_lines ORDER BY line_number", (),
            "quarantine_lines",
            lambda row: {"line_number": row[0], "byte_start": row[1],
                         "byte_end": row[2], "ordinal": row[3],
                         "reason": row[4]})

        # full file diagnostics are themselves bounded by physical lines;
        # stream them rather than holding the list.
        write_json_array(
            fh,
            "SELECT code, message, line_number, byte_start, byte_end, detail "
            "FROM file_diagnostics ORDER BY id", (),
            "file_diagnostics", _diag_row)

        fh.write('  "file_diagnostic_count": '
                 f'{file_diag_total},\n')
        fh.write('  "accepted_details_file": "accepted_details.jsonl",\n')
        fh.write(f'  "accepted_detail_count": {accepted_detail_count},\n')
        fh.write('  "staging": ' + json.dumps({
            "sqlite": "audit.sqlite3",
            "staging_bytes": staging_bytes,
            "peak_rss_kb": _peak_rss_kb(),
        }) + ",\n")
        fh.write(f'  "output_dir": {json.dumps(str(output_dir))}\n')
        fh.write("}\n")


def run_audit(records_path: Union[str, Path],
              quarantine_path: Union[str, Path],
              *, config: AuditConfig,
              expected_source: str,
              expected_lines: int,
              output_dir: Union[str, Path],
              temp_dir: Union[str, Path]) -> dict:
    """Validate one handoff file and materialise its delivery directory.

    The output directory is created only after the whole stream has been
    read and must not already exist, so a failed run can never overwrite a
    previous delivery.  The SQLite staging database lives in a unique
    subdirectory of *temp_dir*; only that subdirectory is removed when the
    run finishes.  Memory usage stays independent of the number of detail
    lines: details, quarantine positions, diagnostics and batch rows are
    staged in SQLite and re-read one row at a time during delivery.
    """
    output_dir = Path(output_dir)
    temp_base = Path(temp_dir)
    temp_base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="audit-staging-", dir=temp_base))
    db_path = staging / "audit.sqlite3"
    try:
        stream = iter_evidence(
            records_path, quarantine_path,
            expected_source=expected_source,
            expected_lines=expected_lines)
        conn = sqlite3.connect(db_path)
        try:
            engine = AuditEngine(config, conn)
            engine.run(stream)
            engine.finish()

            # Reserve the delivery path only after validation completes.
            try:
                output_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise AuditInputError(
                    f"output directory {output_dir} already exists; "
                    "refusing to overwrite an existing delivery")

            accepted_count = _stream_accepted_details(
                output_dir / "accepted_details.jsonl", conn, engine)

            ordinal_rows = conn.execute(
                "SELECT ordinal FROM batch_meta ORDER BY ordinal").fetchall()
            # batch count is a control-record count, never detail-sized;
            # only fixed-size per-batch summaries are retained in memory.
            batch_summaries = [
                _batch_summary(conn, engine, row[0]) for row in ordinal_rows]

            conclusion = PASS
            if engine.file_rejected or any(
                    b["status"] == REJECTED for b in batch_summaries):
                conclusion = BUSINESS_REJECTED

            staging_bytes = sum(
                p.stat().st_size for p in staging.rglob("*")
                if p.is_file())
            _write_report(
                output_dir / "audit_report.json", conn, engine, config,
                expected_source=expected_source,
                expected_lines=expected_lines,
                accepted_detail_count=accepted_count,
                staging_bytes=staging_bytes, output_dir=output_dir,
                batch_summaries=batch_summaries, conclusion=conclusion)

            rejected_batches = sum(
                1 for b in batch_summaries if b["status"] == REJECTED)
            result = {
                "conclusion": conclusion,
                "expected_source": expected_source,
                "expected_lines": expected_lines,
                "file_rejected": engine.file_rejected,
                "batch_count": len(batch_summaries),
                "rejected_batch_count": rejected_batches,
                "accepted_detail_count": accepted_count,
                "accepted_details_file": "accepted_details.jsonl",
                "report_file": "audit_report.json",
                "output_dir": str(output_dir),
                "staging_bytes": staging_bytes,
                "peak_rss_kb": _peak_rss_kb(),
            }
        finally:
            conn.close()
    except AuditInputError:
        # Incomplete evidence: no delivery directory is created and no
        # seemingly-finished result is left behind.
        shutil.rmtree(staging, ignore_errors=True)
        if output_dir.exists() and not any(output_dir.iterdir()):
            output_dir.rmdir()
        raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return result
