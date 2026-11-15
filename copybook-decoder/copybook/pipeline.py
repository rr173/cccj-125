"""File-level pipeline: decode a daily data file with quarantine.

Every physical line is processed independently.  Lines that fail (wrong
length, undecodable field) are isolated into a quarantine list with their
absolute byte offsets; decoding resumes at the next line, so one bad line
never loses the rest of the file.  Line numbers are physical (1-based) and
stay stable regardless of quarantine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from .decoder import Decoder, RedefinesPolicy
from .errors import FieldDecodeError, RecordLengthError
from .manifest import LayoutRef, Manifest
from .registry import LayoutRegistry


@dataclass
class DecodedRecord:
    source_file: str
    line_number: int            # 1-based physical line number
    byte_start: int             # absolute file offset of the first byte
    byte_end: int               # absolute offset one past the last byte
    layout_name: str
    layout_version: str
    fields: dict
    field_errors: list[FieldDecodeError] = dc_field(default_factory=list)
    raw: bytes = b""

    def to_dict(self) -> dict:
        meta = {
            "source_file": self.source_file,
            "line_number": self.line_number,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "layout": self.layout_name,
            "layout_version": self.layout_version,
        }
        if self.field_errors:
            meta["field_errors"] = [
                {"field": e.field, "offset": e.offset, "error": e.reason}
                for e in self.field_errors
            ]
        return {"meta": meta, "fields": _jsonable(self.fields)}


@dataclass
class QuarantinedLine:
    source_file: str
    line_number: int
    byte_start: int
    byte_end: int
    reason: str                 # "truncated" | "overlong" | "decode"
    error: str
    raw: bytes = b""
    error_offset: Optional[int] = None   # absolute offset of the bad field

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "line_number": self.line_number,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "reason": self.reason,
            "error": self.error,
            "error_offset": self.error_offset,
            "raw_hex": self.raw.hex(),
        }


@dataclass
class DecodeResult:
    source_file: str
    layout_ref: LayoutRef
    records: list[DecodedRecord]
    quarantined: list[QuarantinedLine]

    @property
    def ok_count(self) -> int:
        return len(self.records)

    @property
    def quarantine_count(self) -> int:
        return len(self.quarantined)

    @property
    def total_lines(self) -> int:
        return len(self.records) + len(self.quarantined)

    def locate(self, byte_offset: int) -> Union[DecodedRecord, QuarantinedLine, None]:
        """Find the record or quarantined line covering a raw byte offset."""
        for item in (*self.quarantined, *self.records):
            if item.byte_start <= byte_offset < item.byte_end:
                return item
        return None

    def export_quarantine(self, out_dir: Union[str, Path]) -> dict:
        """Write quarantined raw lines plus a JSONL index to *out_dir*.

        Produces ``<stem>.quarantine.bin`` (raw bytes, newline separated)
        and ``<stem>.quarantine.jsonl`` (one index object per line with
        source file, line number, byte offsets and reason).
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(self.source_file).stem
        bin_path = out / f"{stem}.quarantine.bin"
        index_path = out / f"{stem}.quarantine.jsonl"
        with bin_path.open("wb") as fh:
            for q in self.quarantined:
                fh.write(q.raw)
                fh.write(b"\n")
        with index_path.open("w", encoding="utf-8") as fh:
            for q in self.quarantined:
                fh.write(json.dumps(q.to_dict()) + "\n")
        return {"bin": bin_path, "index": index_path}

    def write_jsonl(self, path: Union[str, Path]) -> None:
        """Write decoded records as JSON lines (meta + fields)."""
        with Path(path).open("w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record.to_dict()) + "\n")


def decode_file(path: Union[str, Path], *, sender: str,
                business_date: Union[str, date], manifest: Manifest,
                registry: Optional[LayoutRegistry] = None,
                policy: Optional[RedefinesPolicy] = None,
                copybook: Optional[str] = None) -> DecodeResult:
    """Decode one data file.

    The layout version is resolved from the manifest via
    (sender, business_date); it is never inferred from the file itself.
    """
    path = Path(path)
    ref = manifest.resolve(sender, business_date, copybook)
    if registry is None:
        registry = LayoutRegistry.from_manifest(manifest)
    layout = registry.get(ref.copybook, ref.version)
    decoder = Decoder(layout, policy)

    data = path.read_bytes()
    records: list[DecodedRecord] = []
    quarantined: list[QuarantinedLine] = []

    pos = 0
    line_number = 0
    n = len(data)
    while pos < n:
        nl = data.find(b"\n", pos)
        if nl == -1:
            raw_line, next_pos = data[pos:], n
        else:
            raw_line, next_pos = data[pos:nl], nl + 1
        line_number += 1
        # tolerate CRLF: the \r belongs to the line but not to the record
        content = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        start = pos
        end = pos + len(raw_line)

        if len(content) != layout.record_length:
            reason = "truncated" if len(content) < layout.record_length \
                else "overlong"
            quarantined.append(QuarantinedLine(
                str(path), line_number, start, end, reason,
                f"expected {layout.record_length} bytes, "
                f"got {len(content)}", raw_line))
        else:
            try:
                fields, field_errors = decoder.decode(content)
            except RecordLengthError as exc:  # defensive; pre-checked above
                quarantined.append(QuarantinedLine(
                    str(path), line_number, start, end, "truncated",
                    str(exc), raw_line))
            except FieldDecodeError as exc:
                quarantined.append(QuarantinedLine(
                    str(path), line_number, start, end, "decode",
                    str(exc), raw_line, start + exc.offset))
            else:
                records.append(DecodedRecord(
                    str(path), line_number, start, end,
                    layout.name, layout.version, fields, field_errors,
                    content))
        pos = next_pos

    return DecodeResult(str(path), ref, records, quarantined)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
