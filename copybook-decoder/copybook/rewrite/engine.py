"""Streaming fixed-point rewrite of an original binary file.

The job never re-encodes a whole record.  Each edit becomes a byte-level
requirement over one elementary field span, checked against the original
bytes; bytes no edit covers are copied untouched.  The original is read
in bounded chunks and never held in memory as a whole -- at most one
target record (subject to :data:`DEFAULT_MAX_RECORD_BYTES`) is resident
at a time, alongside the correction order and the compiled layouts.

Passes over the original (always streaming, ``chunk_size`` per read):

1. verify the full-file SHA-256 and every per-record SHA-256 against
   digests computed from the actual bytes, and validate every record
   length against its layout;
2. resolve paths, check every ``expected_hex`` against original bytes,
   encode the new values canonically, and build one byte-requirement map
   per record -- overlapping spans are accepted only where the demanded
   bytes agree;
3. produce the rewritten file by copying the original chunk by chunk and
   overlaying the prepared maps, while re-hashing both files and
   collecting the actual change intervals.

A delivery directory is created only after passes 1-2 succeed.  Any
later failure removes the half-finished directory; the staging area is
always a caller-provided location and only the subdirectory this job
created is cleaned up.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..model import Layout
from .encoder import encode_value
from .errors import (RewriteChecksumError, RewriteConflictError,
                     RewriteError, RewriteIOError, RewriteOrderError,
                     RewriteValueError)
from .order import (CorrectionOrder, LayoutEntry, RecordSpec,
                    is_plain_int, load_order)
from .pathres import resolve_path

DEFAULT_CHUNK_SIZE = 1024 * 1024            # 1 MiB streaming reads
DEFAULT_MAX_RECORD_BYTES = 16 * 1024 * 1024  # one resident record cap
FORMAT = "copybook-rewrite/v1"
OUTPUT_NAME = "original.rewritten.bin"
EVIDENCE_NAME = "rewrite_evidence.json"


@dataclass
class PreparedEdit:
    record: RecordSpec
    layout_key: str
    path: list
    record_start: int
    record_end: int
    field_start: int                 # absolute file offset, inclusive
    field_end: int                   # absolute file offset, exclusive
    old_bytes: bytes
    new_bytes: bytes
    value: object


@dataclass
class PreparedRecord:
    spec: RecordSpec
    layout: Layout
    codec: str
    # absolute file offset -> required new byte
    requirements: dict[int, int] = dc_field(default_factory=dict)
    edits: list[PreparedEdit] = dc_field(default_factory=list)


# ============================================================ public API

def rewrite_original(order_path, *, source_path, output_dir, temp_dir,
                     chunk_size: int = DEFAULT_CHUNK_SIZE,
                     max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
                     copybook_base=None) -> dict:
    """Apply *order_path* to *source_path* and deliver a new directory.

    Returns the evidence summary (the same object written as
    ``rewrite_evidence.json``).  Raises a :class:`RewriteError`
    subclass on any failure, in which case no output directory is left
    behind.
    """
    order = load_order(order_path, copybook_base=copybook_base)
    source = Path(source_path)
    if not source.is_file():
        raise RewriteIOError(f"original file not found: {source}")
    out_dir = Path(output_dir)
    if out_dir.exists():
        raise RewriteIOError(
            f"output directory {out_dir} already exists; refusing to "
            "overwrite an existing delivery")
    if not is_plain_int(chunk_size) or chunk_size <= 0:
        raise RewriteError("chunk_size must be a positive integer")
    if not is_plain_int(max_record_bytes) or max_record_bytes <= 0:
        raise RewriteError("max_record_bytes must be a positive integer")

    staging_base = Path(temp_dir)
    staging_base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="rewrite-staging-",
                                    dir=staging_base))
    output_created = False
    try:
        size = source.stat().st_size
        _check_coordinates(order, size, max_record_bytes)

        # Pass 1: whole-file + per-record digests and record lengths, all
        # computed from the actual bytes (declared values are not trusted).
        actual_full_digest = _verify_digests(source, order, chunk_size)

        # Pass 2: resolve/edit/encode/conflict-check without writing.
        prepared = _prepare_records(source, order, max_record_bytes)

        # From here on the order is fully validated; only I/O can fail.
        try:
            out_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise RewriteIOError(
                f"output directory {out_dir} already exists; refusing to "
                "overwrite an existing delivery")
        output_created = True

        staged_output = staging / "output.bin"
        stats, out_digest, change_intervals = _stream_rewrite(
            source, staged_output, prepared, chunk_size)
        _finalize_delivery(
            staged_output, out_dir, staging, source, order, prepared,
            size, actual_full_digest, out_digest, change_intervals, stats,
            max_record_bytes, chunk_size)
        return _build_evidence(
            source=source, order=order, prepared=prepared, size=size,
            actual_full_digest=actual_full_digest,
            out_digest=out_digest, change_intervals=change_intervals,
            stats=stats, max_record_bytes=max_record_bytes,
            chunk_size=chunk_size)
    except RewriteError:
        if output_created:
            shutil.rmtree(out_dir, ignore_errors=True)
        raise
    except OSError as exc:
        if output_created:
            shutil.rmtree(out_dir, ignore_errors=True)
        raise RewriteIOError(f"I/O failure during rewrite: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ====================================================== coordinate checks

def _check_coordinates(order: CorrectionOrder, file_size: int,
                       max_record_bytes: int) -> None:
    for rec in order.records:
        if rec.byte_end > file_size:
            raise RewriteOrderError(
                f"record span [{rec.byte_start}, {rec.byte_end}) exceeds "
                f"the original file size ({file_size})")
        entry = order.layouts[rec.layout_key]
        if rec.length != entry.layout.record_length:
            raise RewriteOrderError(
                f"record span [{rec.byte_start}, {rec.byte_end}) is "
                f"{rec.length} byte(s) but layout {entry.name} version "
                f"{entry.version} requires {entry.layout.record_length}")
        if rec.length > max_record_bytes:
            raise RewriteError(
                f"record span [{rec.byte_start}, {rec.byte_end}) of "
                f"{rec.length} bytes exceeds the single-record limit "
                f"{max_record_bytes}; raise max_record_bytes explicitly "
                "to allow it")


# =============================================================== pass 1

def _verify_digests(source: Path, order: CorrectionOrder,
                    chunk_size: int) -> str:
    """Stream the original once; verify the full digest and each record's."""
    wanted = sorted(order.records, key=lambda r: r.byte_start)
    record_hashes = [hashlib.sha256() for _ in wanted]
    full = hashlib.sha256()
    position = 0
    idx = 0
    with source.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            full.update(chunk)
            chunk_end = position + len(chunk)
            # Feed every record that intersects this chunk.  A record may
            # span several chunks (its hash object simply keeps state);
            # several small records may also share one chunk.
            while idx < len(wanted) and wanted[idx].byte_start < chunk_end:
                rec = wanted[idx]
                lo = max(rec.byte_start, position) - position
                hi = min(rec.byte_end, chunk_end) - position
                if hi > lo:
                    record_hashes[idx].update(chunk[lo:hi])
                if rec.byte_end <= chunk_end:
                    idx += 1
                else:
                    # This record continues into a later chunk; any later
                    # record starts no earlier (no overlap), so it cannot
                    # intersect the current chunk.
                    break
            position = chunk_end
    actual = full.hexdigest()
    if actual != order.source_sha256:
        raise RewriteChecksumError(
            "the original file's computed SHA-256 "
            f"{actual} does not match the declared source_sha256 "
            f"{order.source_sha256}")
    for rec, h in zip(wanted, record_hashes):
        got = h.hexdigest()
        if got != rec.record_sha256:
            raise RewriteChecksumError(
                f"record span [{rec.byte_start}, {rec.byte_end}): computed "
                f"SHA-256 {got} does not match the declared "
                f"record_sha256 {rec.record_sha256}")
    return actual


# =============================================================== pass 2

def _prepare_records(source: Path, order: CorrectionOrder,
                     max_record_bytes: int) -> list[PreparedRecord]:
    prepared: list[PreparedRecord] = []
    with source.open("rb") as fh:
        for rec in order.records:
            entry = order.layouts[rec.layout_key]
            # Checked against the limit before allocating the record buffer.
            if rec.length > max_record_bytes:  # pragma: no cover - guarded
                raise RewriteError(
                    "record exceeds the single-record size limit")
            fh.seek(rec.byte_start)
            data = _read_exact(fh, rec.length)
            prepared.append(_prepare_one(rec, entry, data))
    return prepared


def _read_exact(fh, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        part = fh.read(remaining)
        if not part:
            raise RewriteIOError(
                "unexpected end of original while reading a target record")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _prepare_one(rec: RecordSpec, entry: LayoutEntry,
                 record_bytes: bytes) -> PreparedRecord:
    layout = entry.layout
    prep = PreparedRecord(spec=rec, layout=layout, codec=entry.codec)
    for edit in rec.edits:
        resolved = resolve_path(layout, edit.raw_path)
        rel_start, rel_end = resolved.start, resolved.end
        if rel_end > len(record_bytes):
            raise RewriteOrderError(
                f"field path {edit.raw_path} resolves outside the record at "
                f"[{rel_start}, {rel_end}) for record "
                f"[{rec.byte_start}, {rec.byte_end})")
        try:
            expected = bytes.fromhex(edit.expected_hex)
        except ValueError:
            raise RewriteOrderError(
                f"expected_hex {edit.expected_hex!r} is not valid hex")
        if len(expected) != (rel_end - rel_start):
            raise RewriteOrderError(
                f"expected_hex for {edit.raw_path} covers {len(expected)} "
                f"byte(s) but the field spans {rel_end - rel_start}")
        original_field = record_bytes[rel_start:rel_end]
        if expected != original_field:
            raise RewriteChecksumError(
                f"expected_hex for {edit.raw_path} on record "
                f"[{rec.byte_start}, {rec.byte_end}) does not match the "
                f"original bytes: expected {edit.expected_hex}, found "
                f"{original_field.hex()}")
        new_bytes = encode_value(edit.value, resolved.field, entry.codec)
        if len(new_bytes) != (rel_end - rel_start):
            raise RewriteValueError(
                f"encoded value for {edit.raw_path} is {len(new_bytes)} "
                f"byte(s) but the field span is {rel_end - rel_start}")
        abs_start = rec.byte_start + rel_start
        abs_end = rec.byte_start + rel_end
        for pos, byte in zip(range(abs_start, abs_end), new_bytes):
            existing = prep.requirements.get(pos)
            if existing is not None and existing != byte:
                raise RewriteConflictError(
                    f"conflicting edits at absolute byte {pos}: demands "
                    f"0x{existing:02x} and 0x{byte:02x}; overlapping field "
                    "spans must request identical bytes for the shared "
                    "positions")
            prep.requirements[pos] = byte
        prep.edits.append(PreparedEdit(
            record=rec, layout_key=rec.layout_key, path=resolved.path,
            record_start=rec.byte_start, record_end=rec.byte_end,
            field_start=abs_start, field_end=abs_end,
            old_bytes=bytes(original_field), new_bytes=new_bytes,
            value=edit.value))
    # Canonical evidence order for this record, independent of input order.
    prep.edits.sort(key=lambda e: (e.field_start, e.field_end,
                                   _path_key(e.path)))
    return prep


def _path_key(path) -> tuple:
    return tuple((0, c) if isinstance(c, str) else (1, c) for c in path)


# =============================================================== pass 3

def _stream_rewrite(source: Path, destination: Path,
                    prepared: list[PreparedRecord], chunk_size: int):
    """Copy original -> destination with byte overlays, re-hashing both."""
    # Flatten requirements into one sorted, non-overlapping logical map.
    overlay: dict[int, int] = {}
    for prep in prepared:
        for pos, byte in prep.requirements.items():
            overlay[pos] = byte       # overlaps already proven consistent
    overlay_positions = sorted(overlay)

    in_hash = hashlib.sha256()
    out_hash = hashlib.sha256()
    covered = set(overlay)
    changed_positions: set[int] = set()
    cursor = 0
    next_overlay = 0
    out = destination.open("wb")
    try:
        with source.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                in_hash.update(chunk)
                buf = bytearray(chunk)
                chunk_end = cursor + len(buf)
                while (next_overlay < len(overlay_positions)
                       and overlay_positions[next_overlay] < chunk_end):
                    pos = overlay_positions[next_overlay]
                    local = pos - cursor
                    new_byte = overlay[pos]
                    if buf[local] != new_byte:
                        changed_positions.add(pos)
                        buf[local] = new_byte
                    next_overlay += 1
                out.write(bytes(buf))
                out_hash.update(bytes(buf))
                cursor = chunk_end
        out.flush()
        os.fsync(out.fileno())
    finally:
        out.close()

    # Every change must lie inside an edit span; every covered byte is
    # either changed or provably equal to the original.
    if changed_positions - covered:
        raise RewriteError("internal error: a changed byte is outside all "
                           "edit spans")  # pragma: no cover
    intervals = _merge_change_intervals(
        source, destination, changed_positions, chunk_size)
    stats = {
        "requested_field_count": sum(len(p.edits) for p in prepared),
        "covered_byte_count": len(covered),
        "changed_byte_count": len(changed_positions),
    }
    return stats, out_hash.hexdigest(), intervals


def _merge_change_intervals(source: Path, destination: Path,
                            changed_positions: set[int],
                            chunk_size: int) -> list[dict]:
    """Group sorted changed positions into adjacent intervals, and record
    the original/new bytes of each interval read back from the files."""
    if not changed_positions:
        return []
    ordered = sorted(changed_positions)
    spans: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for pos in ordered[1:]:
        if pos == prev + 1:
            prev = pos
            continue
        spans.append((start, prev + 1))
        start = prev = pos
    spans.append((start, prev + 1))

    intervals: list[dict] = []
    with source.open("rb") as src, destination.open("rb") as dst:
        for lo, hi in spans:
            src.seek(lo)
            dst.seek(lo)
            length = hi - lo
            before = _read_exact(src, length)
            after = _read_exact(dst, length)
            intervals.append({
                "byte_start": lo,
                "byte_end": hi,
                "before_hex": before.hex(),
                "after_hex": after.hex(),
            })
    return intervals


# ============================================================== delivery

def _finalize_delivery(staged_output: Path, out_dir: Path,
                       staging: Path, source: Path, order: CorrectionOrder,
                       prepared, size, actual_full_digest, out_digest,
                       change_intervals, stats, max_record_bytes,
                       chunk_size) -> None:
    if staged_output.stat().st_size != size:
        raise RewriteIOError(
            "rewritten file length differs from the original; aborting")
    # Independent re-read of the delivered bytes after staging.
    re_hash = hashlib.sha256()
    with staged_output.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            re_hash.update(block)
    if re_hash.hexdigest() != out_digest:
        raise RewriteIOError(
            "rewritten file digest changed between write and verification")
    target = out_dir / OUTPUT_NAME
    shutil.move(str(staged_output), str(target))
    evidence = _build_evidence(
        source=source, order=order, prepared=prepared, size=size,
        actual_full_digest=actual_full_digest,
        out_digest=out_digest, change_intervals=change_intervals,
        stats=stats, max_record_bytes=max_record_bytes,
        chunk_size=chunk_size)
    evidence_path = out_dir / EVIDENCE_NAME
    with evidence_path.open("w", encoding="utf-8") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=2,
                  sort_keys=False)
        fh.write("\n")


def _build_evidence(*, source: Path, order: CorrectionOrder, prepared,
                    size, actual_full_digest, out_digest,
                    change_intervals, stats, max_record_bytes,
                    chunk_size) -> dict:
    layout_section = {}
    for key in sorted(order.layouts):
        entry = order.layouts[key]
        layout_section[key] = {
            "copybook_path": str(entry.copybook_path),
            "copybook_sha256": entry.copybook_sha256,
            "layout_name": entry.name,
            "layout_version": entry.version,
            "codec": entry.codec,
            "record_length": entry.layout.record_length,
        }
    edits_section = []
    for prep in sorted(prepared, key=lambda p: p.spec.byte_start):
        for edit in prep.edits:
            edits_section.append({
                "path": list(edit.path),
                "record_byte_start": edit.record_start,
                "record_byte_end": edit.record_end,
                "layout": edit.layout_key,
                "field_byte_start": edit.field_start,
                "field_byte_end": edit.field_end,
                "old_hex": edit.old_bytes.hex(),
                "requested_hex": edit.new_bytes.hex(),
                "value": edit.value,
            })
    return {
        "format": FORMAT,
        "input": {
            "path": str(source),
            "sha256": actual_full_digest,
            "length_bytes": size,
        },
        "output": {
            "file": OUTPUT_NAME,
            "sha256": out_digest,
            "length_bytes": size,
        },
        "layouts": layout_section,
        "declared_source_sha256": order.source_sha256,
        "record_count": len(order.records),
        "edits": edits_section,
        "change_intervals": change_intervals,
        "counts": {
            "requested_fields": stats["requested_field_count"],
            "covered_bytes": stats["covered_byte_count"],
            "changed_bytes": stats["changed_byte_count"],
        },
        "limits": {
            "chunk_size": chunk_size,
            "max_record_bytes": max_record_bytes,
        },
    }
