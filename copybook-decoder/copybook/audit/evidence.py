"""Streaming evidence merge.

The auditor consumes exactly two artifacts produced by the existing
pipeline:

* the normal-record JSONL from :meth:`DecodeResult.write_jsonl` -- each
  object is ``{"meta": {...}, "fields": {...}}``;
* the quarantine index JSONL from
  :meth:`DecodeResult.export_quarantine` -- each object carries
  ``raw_hex`` but **no** ``fields`` and must never be re-parsed as a
  business record.

Both files are already ordered by physical source line.  They are merged
with a two-pointer walk that holds at most one parsed object per input in
memory.  The merge proves that physical lines ``1 .. expected_lines`` are
covered exactly once: quarantine lines leave holes in the normal output but
must never renumber the lines that follow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from .errors import AuditInputError


@dataclass
class Evidence:
    """One physical source line, as proven by the two input files."""

    kind: str            # "record" (decoded normal line) or "quarantine"
    line_number: int
    byte_start: int
    byte_end: int
    source_file: str
    fields: Optional[dict]   # None for quarantine evidence
    raw: dict                # the original JSON object, for diagnostics


class _JsonlSource:
    """Lazy, one-line-at-a-time reader over one JSONL evidence file."""

    def __init__(self, path: Union[str, Path], kind: str):
        self.path = Path(path)
        self.kind = kind
        try:
            self._fh = self.path.open("r", encoding="utf-8")
        except OSError as exc:
            raise AuditInputError(f"cannot open evidence file {self.path}: {exc}")
        self._seq = 0
        self.head: Optional[Evidence] = None
        self.advance()

    def advance(self) -> None:
        line = self._fh.readline()
        if line == "":
            self.head = None
            return
        self._seq += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditInputError(
                f"{self.path} entry #{self._seq} is not valid JSON: {exc}")
        self.head = self._to_evidence(obj)

    def close(self) -> None:
        self._fh.close()

    def _to_evidence(self, obj: Any) -> Evidence:
        where = f"{self.path} entry #{self._seq}"
        if not isinstance(obj, dict):
            raise AuditInputError(f"{where}: expected a JSON object")

        if self.kind == "record":
            meta = obj.get("meta")
            fields = obj.get("fields")
            if not isinstance(meta, dict) or not isinstance(fields, dict):
                raise AuditInputError(
                    f"{where}: normal records need 'meta' and 'fields' objects")
            if "raw_hex" in obj or "raw_hex" in meta:
                # A quarantine index line must not be smuggled in as a
                # decodable business record.
                raise AuditInputError(
                    f"{where}: normal record must not carry raw_hex; "
                    "quarantine evidence belongs in the quarantine index")
            source = meta.get("source_file")
            line_number = meta.get("line_number")
            byte_start = meta.get("byte_start")
            byte_end = meta.get("byte_end")
        else:
            if "fields" in obj:
                raise AuditInputError(
                    f"{where}: quarantine index entries must not carry "
                    "'fields'; the raw line is not re-decoded")
            source = obj.get("source_file")
            line_number = obj.get("line_number")
            byte_start = obj.get("byte_start")
            byte_end = obj.get("byte_end")
            fields = None

        if not isinstance(source, str) or not source:
            raise AuditInputError(f"{where}: missing string 'source_file'")
        if not isinstance(line_number, int) or isinstance(line_number, bool):
            raise AuditInputError(f"{where}: line_number must be an integer")
        if not isinstance(byte_start, int) or isinstance(byte_start, bool) \
                or not isinstance(byte_end, int) or isinstance(byte_end, bool):
            raise AuditInputError(
                f"{where}: byte_start/byte_end must be integers")
        if byte_end < byte_start or byte_start < 0:
            raise AuditInputError(
                f"{where}: inverted or negative byte range "
                f"[{byte_start}, {byte_end})")
        return Evidence(self.kind, line_number, byte_start, byte_end,
                        source, fields, obj)


def iter_evidence(records_path: Union[str, Path],
                  quarantine_path: Union[str, Path],
                  *, expected_source: str,
                  expected_lines: int) -> Iterator[Evidence]:
    """Yield every physical line 1..expected_lines exactly once, in order.

    Raises :class:`AuditInputError` for duplicates, gaps, out-of-range line
    numbers, source mismatches, unsorted inputs or backwards byte ranges.
    """
    if not isinstance(expected_lines, int) or isinstance(expected_lines, bool) \
            or expected_lines <= 0:
        raise AuditInputError(
            f"expected_lines must be a positive integer, got {expected_lines!r}")
    if not isinstance(expected_source, str) or not expected_source:
        raise AuditInputError("expected_source must be a non-empty string")

    records = _JsonlSource(records_path, "record")
    quarantined = _JsonlSource(quarantine_path, "quarantine")
    try:
        yield from _merge(records, quarantined,
                          expected_source=expected_source,
                          expected_lines=expected_lines)
    finally:
        records.close()
        quarantined.close()


def _merge(records: _JsonlSource, quarantined: _JsonlSource, *,
           expected_source: str, expected_lines: int) -> Iterator[Evidence]:
    seen = 0
    last_end = -1
    last_ev: Optional[Evidence] = None

    def check(ev: Evidence) -> None:
        nonlocal last_end
        if ev.source_file != expected_source:
            raise AuditInputError(
                f"line {ev.line_number}: source_file {ev.source_file!r} does "
                f"not match expected_source {expected_source!r}")
        if ev.line_number < 1 or ev.line_number > expected_lines:
            raise AuditInputError(
                f"line number {ev.line_number} is outside 1..{expected_lines}")
        if ev.line_number != seen + 1:
            # Catches both gaps and duplicates (a duplicate would also be
            # caught here because the counter cannot advance twice).
            raise AuditInputError(
                f"line coverage broken after physical line {seen}: found "
                f"{ev.line_number}; lines 1..{expected_lines} must each "
                "appear exactly once")
        if ev.byte_start < last_end:
            raise AuditInputError(
                f"line {ev.line_number} byte range [{ev.byte_start}, "
                f"{ev.byte_end}) starts before the previous range ended at "
                f"{last_end} (line {last_ev.line_number})")

    while records.head is not None or quarantined.head is not None:
        r = records.head
        q = quarantined.head
        if q is None or (r is not None and r.line_number <= q.line_number):
            ev = r
            # Same line present in both inputs = duplicated evidence.
            if q is not None and q.line_number == ev.line_number:
                raise AuditInputError(
                    f"physical line {ev.line_number} appears in both the "
                    "normal records and the quarantine index")
            records.advance()
        else:
            ev = q
            quarantined.advance()
        check(ev)
        seen += 1
        last_end = ev.byte_end
        last_ev = ev
        yield ev

    if seen != expected_lines:
        raise AuditInputError(
            f"evidence covers {seen} physical line(s) but expected_lines="
            f"{expected_lines}; the proof set is incomplete")
