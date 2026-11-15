"""Low-level Git bundle v2 container parsing and pack frame validation.

This module never decodes object contents.  Its job is to prove, using
only bounded memory, that:

* the bundle header is a supported self-contained v2 header (SHA-1
  object ids, commit refs only, no prerequisites, no capabilities);
* the embedded pack starts right after the header and its PACK
  signature/version/object count are correct;
* every zlib stream ends exactly where the next entry begins and inflates
  to exactly the declared size, within the configured limits;
* the pack's own SHA-1 trailer matches its bytes;
* the bundle's final 20 bytes are the SHA-1 of everything before them and
  there are no bytes after the trailer (an extra byte, however small,
  means an incomplete delivery).

Only then does :func:`parse_pack` report entry offsets; object extraction
is streamed from disk and never keeps the whole pack or all inflated
objects in memory.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional, Union

from .errors import (BundleFormatError, ObjectFormatError,
                     PackFormatError)

BUNDLE_SIGNATURE = b"# v2 git bundle"
PACK_SIGNATURE = b"PACK"
PACK_VERSION = 2

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7
KNOWN_TYPES = frozenset({OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG,
                         OBJ_OFS_DELTA, OBJ_REF_DELTA})
TYPE_NAMES = {OBJ_COMMIT: "commit", OBJ_TREE: "tree", OBJ_BLOB: "blob",
              OBJ_TAG: "tag", OBJ_OFS_DELTA: "ofs-delta",
              OBJ_REF_DELTA: "ref-delta"}

#: I/O window for streaming.  Decompression is bounded independently.
CHUNK = 64 * 1024
#: Upper bound on the textual bundle header (ref listing).
MAX_HEADER_BYTES = 16 * 1024 * 1024
#: Upper bound on one header/ref line.
MAX_LINE_BYTES = 1024 * 1024
HEX_CHARS = frozenset("0123456789abcdef")


def _is_hex(text: str, length: int) -> bool:
    return len(text) == length and all(c in HEX_CHARS for c in text)


@dataclass(frozen=True)
class BundleRef:
    """One ref advertised by the bundle header."""

    name: str
    object_id: str

    def as_dict(self) -> dict:
        return {"name": self.name, "object_id": self.object_id}


@dataclass
class BundleLayout:
    """Header and trailer facts of one validated bundle."""

    path: str
    digest: str                     # SHA-1 of the whole bundle file
    size: int
    refs: list[BundleRef]
    header_bytes: int               # file offset of the pack
    pack_offset: int
    pack_bytes: int                 # embedded pack incl. its 20-byte
                                    # trailer; 0 for a header-only bundle
    object_count: int               # from the PACK header (0 when empty)
    trailer: str                    # hex SHA-1 stored in the bundle tail

    @property
    def ref_names(self) -> list[str]:
        return [ref.name for ref in self.refs]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "sha1": self.digest,
            "size": self.size,
            "header_bytes": self.header_bytes,
            "pack_offset": self.pack_offset,
            "pack_bytes": self.pack_bytes,
            "pack_object_count": self.object_count,
            "trailer_sha1": self.trailer,
            "refs": [ref.as_dict() for ref in self.refs],
        }


@dataclass(frozen=True)
class PackEntry:
    """Frame facts of one object inside the pack (no content)."""

    index: int
    offset: int                     # absolute offset of the entry header
    obj_type: int
    size: int                       # declared *inflated* size
    header_len: int                 # entry header bytes incl. base ref
    stream_offset: int              # absolute offset of zlib data
    compressed_size: int
    base_distance: Optional[int] = None   # OFS_DELTA
    base_id: Optional[str] = None         # REF_DELTA (40-hex)

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.obj_type]

    @property
    def is_delta(self) -> bool:
        return self.obj_type in (OBJ_OFS_DELTA, OBJ_REF_DELTA)

    @property
    def end_offset(self) -> int:
        return self.stream_offset + self.compressed_size


@dataclass
class PackLayout:
    path: str
    version: int
    object_count: int
    entries: list[PackEntry]
    trailer: str
    data_end: int                   # offset where the trailer starts
    #: absolute entry offset -> index, used to resolve OFS_DELTA bases.
    offset_index: dict[int, int] = field(default_factory=dict)


class _ForwardReader:
    """Sequential reader over [start, end) with running SHA-1."""

    def __init__(self, fh: BinaryIO, start: int, end: int):
        self.fh = fh
        self.end = end
        self.pos = start
        fh.seek(start)

    def read(self, n: int) -> bytes:
        if self.pos >= self.end:
            return b""
        data = self.fh.read(min(n, self.end - self.pos))
        self.pos += len(data)
        return data

    def remaining(self) -> int:
        return self.end - self.pos


def read_bundle_layout(path: Union[str, Path]) -> BundleLayout:
    """Validate the bundle header/trailer; do not parse the pack yet."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        reader = _ForwardReader(fh, 0, size)
        first = _readline(reader, MAX_LINE_BYTES)
        if first != BUNDLE_SIGNATURE + b"\n":
            raise BundleFormatError(
                "not a supported bundle: first line must be "
                f"'# v2 git bundle' (got {first[:80]!r})")
        header_len = len(first)
        refs: list[BundleRef] = []
        seen_names: set[str] = set()
        while True:
            if header_len > MAX_HEADER_BYTES:
                raise BundleFormatError(
                    f"bundle header exceeds {MAX_HEADER_BYTES} bytes")
            line = _readline(reader, MAX_LINE_BYTES)
            if not line:
                raise BundleFormatError(
                    "bundle header is not terminated by an empty line")
            header_len += len(line)
            if line == b"\n":
                break
            ref = _parse_ref_line(line)
            if ref.name in seen_names:
                raise BundleFormatError(
                    f"bundle advertises ref {ref.name!r} more than once")
            seen_names.add(ref.name)
            refs.append(ref)
        pack_offset = reader.pos

        if size - pack_offset < 20:
            raise BundleFormatError(
                "bundle tail is truncated: no 20-byte SHA-1 trailer")
        trailer_hex, pack_bytes, object_count = _check_pack_frame(
            reader, size, pack_offset)
        _verify_bundle_sha1(path, size, pack_offset, trailer_hex,
                            pack_bytes)
        digest = _hash_file(path, size)
        return BundleLayout(
            path=str(path), digest=digest, size=size, refs=refs,
            header_bytes=header_len, pack_offset=pack_offset,
            pack_bytes=pack_bytes, object_count=object_count,
            trailer=trailer_hex)


def _readline(reader: _ForwardReader, limit: int) -> bytes:
    out = bytearray()
    while True:
        byte = reader.read(1)
        if not byte:
            return bytes(out)
        out += byte
        if byte == b"\n":
            return bytes(out)
        if len(out) > limit:
            raise BundleFormatError(
                f"bundle header line exceeds {limit} bytes")


def _parse_ref_line(line: bytes) -> BundleRef:
    if line.startswith(b"@"):
        raise BundleFormatError(
            f"unsupported bundle capability line {line.strip()!r}")
    body = line[:-1] if line.endswith(b"\n") else line
    if body.startswith(b"-"):
        raise BundleFormatError(
            "incremental bundle with prerequisites is not supported: "
            f"{body[:80]!r}")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise BundleFormatError(
            "bundle ref line is not valid UTF-8") from None
    if not text or text[0] == "#":
        raise BundleFormatError(
            f"unsupported bundle header line {text[:80]!r}")
    parts = text.split(" ")
    if len(parts) != 2:
        raise BundleFormatError(
            f"malformed bundle ref line {text[:120]!r}")
    oid, name = parts
    if not _is_hex(oid, 40):
        if _is_hex(oid, 64):
            raise BundleFormatError(
                "unsupported object format: 64-hex object ids "
                "(SHA-256 is not supported; SHA-1 only)")
        raise BundleFormatError(
            f"ref object id is not a 40-hex SHA-1: {oid[:80]!r}")
    if not name:
        raise BundleFormatError("bundle ref has an empty name")
    _validate_refname(name)
    return BundleRef(name=name, object_id=oid)


_FORBIDDEN_REF_CHARS = set(" ~^:?*[\\\x7f")


def _validate_refname(name: str) -> None:
    """Reject names Git itself would refuse (see git-check-ref-format)."""
    if not (name == "HEAD" or name.startswith("refs/")):
        raise BundleFormatError(
            f"bundle ref {name!r} is not a full refname (refs/...) or HEAD")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in name):
        raise BundleFormatError(
            f"bundle ref {name!r} contains a control character")
    if any(c in _FORBIDDEN_REF_CHARS for c in name):
        raise BundleFormatError(
            f"bundle ref {name!r} contains a forbidden character")
    if name.startswith("/") or name.endswith("/") or "//" in name:
        raise BundleFormatError(f"bundle ref {name!r} has an empty component")
    if name.endswith(".") or ".." in name or "@{" in name:
        raise BundleFormatError(f"bundle ref {name!r} is malformed")
    for component in name.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            raise BundleFormatError(
                f"bundle ref {name!r} has an invalid component "
                f"{component!r}")


def _check_pack_frame(reader: _ForwardReader, size: int,
                      pack_offset: int) -> tuple[str, int, int]:
    """Check the PACK header when present; return (trailer, bytes, count)."""
    if size - pack_offset == 20:
        # A header with no pack at all carries no objects; a self-contained
        # delivery cannot be verified and is refused here.
        raise BundleFormatError(
            "bundle contains no pack: a self-contained delivery must carry "
            "its objects")
    header = reader.read(12)
    if len(header) != 12:
        raise BundleFormatError(
            "bundle is truncated before the pack header")
    if header[:4] != PACK_SIGNATURE:
        raise PackFormatError(
            f"pack signature is not 'PACK' at bundle offset "
            f"{pack_offset}: {header[:4]!r}")
    version, count = struct.unpack(">II", header[4:])
    if version != PACK_VERSION:
        raise PackFormatError(
            f"unsupported pack version {version} (expected 2)")
    pack_bytes = size - pack_offset
    if pack_bytes < 12 + 20:
        raise PackFormatError(
            "pack is shorter than its header and checksum")
    reader.fh.seek(size - 20)
    trailer = reader.fh.read(20)
    if len(trailer) != 20:
        raise BundleFormatError("bundle tail is truncated")
    return trailer.hex(), pack_bytes, count


def _verify_bundle_sha1(path: Path, size: int, pack_offset: int,
                        trailer_hex: str, pack_span: int) -> None:
    """The trailing 20 bytes must be the pack's own SHA-1.

    Git writes the pack checksum as the final 20 bytes of the bundle; the
    hash covers the pack bytes from the ``PACK`` signature up to (not
    including) that trailer.  A bundle with bytes after the trailer
    therefore cannot satisfy this check: the last 20 bytes are no longer
    the pack checksum.
    """
    pack_len = pack_span - 20
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        fh.seek(pack_offset)
        remaining = pack_len
        while remaining:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                raise BundleFormatError("bundle is truncated")
            digest.update(chunk)
            remaining -= len(chunk)
    actual = digest.hexdigest()
    if actual != trailer_hex:
        extra = _count_trailing_bytes(path, pack_offset, size, pack_span)
        if extra is not None:
            raise BundleFormatError(
                f"bundle has {extra} byte(s) after its trailing SHA-1; a "
                "complete delivery ends exactly at the pack checksum")
        raise BundleFormatError(
            f"bundle trailing SHA-1 mismatch: stored {trailer_hex}, "
            f"computed {actual}")


def _count_trailing_bytes(path: Path, pack_offset: int, size: int,
                          pack_span: int) -> Optional[int]:
    """How many bytes follow the real pack trailer, if any.

    The bundle's last 20 bytes must be the pack SHA-1.  When a short tail
    was appended, the genuine trailer sits ``k`` bytes before it.  This
    scans a bounded tail incrementally: after hashing the pack prefix up to
    each candidate boundary, the next 20 bytes are compared with the
    running digest.  Returning ``k`` lets the error say the delivery has
    trailing bytes instead of only reporting a checksum mismatch.
    """
    if pack_span < 40:
        return None
    scan = min(4096, pack_span - 20)
    prefix_len = pack_span - 20 - scan
    hasher = hashlib.sha1()
    with path.open("rb") as fh:
        fh.seek(pack_offset)
        remaining = prefix_len
        while remaining:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                return None
            hasher.update(chunk)
            remaining -= len(chunk)
        for step in range(scan):
            boundary = fh.tell()
            stored = fh.read(20)
            if len(stored) != 20:
                return None
            if hasher.digest() == stored:
                trailing = (size - 20) - boundary
                return trailing if trailing > 0 else None
            fh.seek(boundary)
            one = fh.read(1)
            if not one:
                return None
            hasher.update(one)
    return None


def _hash_file(path: Path, size: int) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        remaining = size
        while remaining:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                raise BundleFormatError("bundle is truncated")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def extract_pack(layout: BundleLayout, dest: Union[str, Path]) -> int:
    """Copy the embedded pack bytes to *dest* (the caller cleans up)."""
    dest = Path(dest)
    if layout.pack_bytes == 0:
        dest.write_bytes(b"")
        return 0
    with open(layout.path, "rb") as src, open(dest, "wb") as out:
        src.seek(layout.pack_offset)
        remaining = layout.pack_bytes
        while remaining:
            chunk = src.read(min(1 << 20, remaining))
            if not chunk:
                raise BundleFormatError(
                    "bundle shrank while extracting the pack")
            out.write(chunk)
            remaining -= len(chunk)
    if dest.stat().st_size != layout.pack_bytes:
        raise BundleFormatError("bundle pack copy has the wrong size")
    return layout.pack_bytes


def parse_pack(pack_path: Union[str, Path], *,
               max_stream: int, max_total: int) -> PackLayout:
    """Validate every pack frame and stream, then return its entry map.

    *max_stream* bounds one inflated object (and its compressed stream);
    *max_total* bounds the cumulative inflated bytes of the whole pack.
    Exceeding either is a hard refusal -- never a truncated object.
    """
    pack_path = Path(pack_path)
    size = pack_path.stat().st_size
    if size < 12 + 20:
        raise PackFormatError(
            "pack is shorter than its header and checksum")
    with pack_path.open("rb") as fh:
        header = fh.read(12)
        if header[:4] != PACK_SIGNATURE:
            raise PackFormatError("pack signature is not 'PACK'")
        version, count = struct.unpack(">II", header[4:])
        if version != PACK_VERSION:
            raise PackFormatError(
                f"unsupported pack version {version} (expected 2)")
        data_end = size - 20
        _verify_pack_sha1(fh, size)
        entries: list[PackEntry] = []
        offset = 12
        total = 0
        while offset < data_end:
            entry, total = _parse_entry(fh, pack_path, offset, len(entries),
                                        data_end, max_stream, max_total,
                                        total)
            entries.append(entry)
            offset = entry.end_offset
        if offset != data_end:
            raise PackFormatError(
                f"pack data ends at {offset} but its checksum starts at "
                f"{data_end}")
        if len(entries) != count:
            raise PackFormatError(
                f"pack declares {count} objects but contains "
                f"{len(entries)}")
        return PackLayout(
            path=str(pack_path), version=version, object_count=count,
            entries=entries, trailer=_read_trailer(fh, size),
            data_end=data_end,
            offset_index={e.offset: e.index for e in entries})


def _read_trailer(fh: BinaryIO, size: int) -> str:
    fh.seek(size - 20)
    raw = fh.read(20)
    if len(raw) != 20:
        raise PackFormatError("pack trailer is truncated")
    return raw.hex()


def _verify_pack_sha1(fh: BinaryIO, size: int) -> None:
    """The pack trailer must hash the pack without its last 20 bytes."""
    fh.seek(size - 20)
    stored = fh.read(20)
    digest = hashlib.sha1()
    fh.seek(0)
    remaining = size - 20
    while remaining:
        chunk = fh.read(min(1 << 20, remaining))
        if not chunk:
            raise PackFormatError("pack is truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if digest.digest() != stored:
        raise PackFormatError(
            f"pack SHA-1 mismatch: stored {stored.hex()}, computed "
            f"{digest.hexdigest()}")


def _parse_entry(fh: BinaryIO, path: Path, offset: int, index: int,
                 data_end: int, max_stream: int, max_total: int,
                 total_before: int) -> tuple[PackEntry, int]:
    fh.seek(offset)
    head = fh.read(1)
    if not head:
        raise PackFormatError(
            f"pack truncated at object {index} header")
    c = head[0]
    obj_type = (c >> 4) & 0x07
    declared = c & 0x0F
    shift = 4
    header_len = 1
    while c & 0x80:
        more = fh.read(1)
        if not more:
            raise PackFormatError(
                f"pack truncated in object {index} size header")
        c = more[0]
        header_len += 1
        if header_len > 10:
            raise PackFormatError(
                f"object {index} has an overlong size header")
        declared |= (c & 0x7F) << shift
        shift += 7
    if obj_type not in KNOWN_TYPES:
        raise PackFormatError(
            f"object {index} at offset {offset} has unknown pack type "
            f"{obj_type}")
    if declared > max_stream:
        raise PackFormatError(
            f"object {index} declares {declared} bytes, past the "
            f"{max_stream}-byte single-object limit")
    if obj_type == OBJ_TAG:
        raise ObjectFormatError(
            f"object {index} at offset {offset} is an annotated tag: only "
            "commit, tree and blob objects are supported")
    base_distance = None
    base_id = None
    if obj_type == OBJ_OFS_DELTA:
        base_distance, extra = _read_ofs_base(fh, index)
        header_len += extra
    elif obj_type == OBJ_REF_DELTA:
        raw = fh.read(20)
        if len(raw) != 20:
            raise PackFormatError(
                f"pack truncated in REF_DELTA base of object {index}")
        base_id = raw.hex()
        header_len += 20
    stream_offset = fh.tell()
    produced, compressed = _inflate_stream(
        fh, stream_offset, data_end, index, declared, max_stream,
        max_total - total_before)
    if produced != declared:
        raise PackFormatError(
            f"object {index} declares {declared} bytes but its zlib "
            f"stream inflates to {produced}")
    end = stream_offset + compressed
    if end > data_end:
        raise PackFormatError(
            f"object {index} stream runs past the pack trailer")
    return (PackEntry(
        index=index, offset=offset, obj_type=obj_type, size=declared,
        header_len=header_len, stream_offset=stream_offset,
        compressed_size=compressed, base_distance=base_distance,
        base_id=base_id), total_before + produced)


def _inflate_stream(fh: BinaryIO, start: int, data_end: int, index: int,
                    declared: int, max_stream: int,
                    remaining_budget: int) -> tuple[int, int]:
    """Inflate one zlib stream from *fh* and return (size, consumed)."""
    inflater = zlib.decompressobj()
    read_pos = start
    produced = 0
    consumed = 0
    limit = min(max_stream, declared + 1)
    while not inflater.eof:
        if read_pos >= data_end:
            raise PackFormatError(
                f"object {index} zlib stream is truncated at the pack "
                "trailer")
        chunk = fh.read(min(CHUNK, data_end - read_pos))
        if not chunk:
            raise PackFormatError(
                f"object {index} zlib stream is truncated")
        read_pos += len(chunk)
        consumed += len(chunk)
        if consumed > max_stream:
            raise PackFormatError(
                f"object {index} compressed stream exceeds the "
                f"{max_stream}-byte single-object limit")
        try:
            out = inflater.decompress(chunk, limit - produced + 1)
            produced += len(out)
            if produced > declared:
                raise PackFormatError(
                    f"object {index} inflates past its declared size "
                    f"{declared}")
            if produced > remaining_budget:
                raise PackFormatError(
                    "cumulative inflated object bytes exceed the "
                    "configured limit")
            while inflater.unconsumed_tail and not inflater.eof:
                # ``max_length`` cut the output short; keep draining.
                out = inflater.decompress(inflater.unconsumed_tail,
                                          limit - produced + 1)
                produced += len(out)
                if produced > declared:
                    raise PackFormatError(
                        f"object {index} inflates past its declared size "
                        f"{declared}")
                if produced > remaining_budget:
                    raise PackFormatError(
                        "cumulative inflated object bytes exceed the "
                        "configured limit")
        except zlib.error as exc:
            raise PackFormatError(
                f"object {index} zlib stream is corrupt: {exc}") from None
    tail = inflater.unused_data
    if tail:
        consumed -= len(tail)
        if consumed < 0:
            raise PackFormatError(
                f"object {index} zlib stream boundary is inconsistent")
    return produced, consumed


def _read_ofs_base(fh: BinaryIO, index: int) -> tuple[int, int]:
    """Consume an OFS_DELTA distance; return (distance, bytes consumed)."""
    raw = fh.read(1)
    if not raw:
        raise PackFormatError(
            f"pack truncated in OFS_DELTA base of object {index}")
    consumed = 1
    c = raw[0]
    value = c & 0x7F
    while c & 0x80:
        raw = fh.read(1)
        if not raw:
            raise PackFormatError(
                f"pack truncated in OFS_DELTA base of object {index}")
        consumed += 1
        if consumed > 10:
            raise PackFormatError(
                f"object {index} has an overlong OFS_DELTA base")
        c = raw[0]
        value = ((value + 1) << 7) | (c & 0x7F)
    return value, consumed


def read_entry_stream(pack_path: Union[str, Path], entry: PackEntry,
                      *,
                      max_bytes: int) -> bytes:
    """Inflate one pack entry, enforcing *max_bytes* exactly.

    Used for delta payloads and for re-materialising a base object on
    demand.  The caller knows the declared size; more output than that is
    a corruption, not something to truncate.
    """
    pack_path = Path(pack_path)
    with pack_path.open("rb") as fh:
        fh.seek(entry.stream_offset)
        inflater = zlib.decompressobj()
        out = bytearray()
        read_pos = entry.stream_offset
        while not inflater.eof:
            chunk = fh.read(min(CHUNK, entry.compressed_size -
                                (read_pos - entry.stream_offset)))
            if not chunk:
                raise PackFormatError(
                    f"object {entry.index} zlib stream is truncated")
            read_pos += len(chunk)
            try:
                out += inflater.decompress(chunk, max_bytes - len(out) + 1)
            except zlib.error as exc:
                raise PackFormatError(
                    f"object {entry.index} zlib stream is corrupt: "
                    f"{exc}") from None
            if len(out) > max_bytes:
                raise PackFormatError(
                    f"object {entry.index} inflates past the "
                    f"{max_bytes}-byte limit")
            if inflater.unused_data:
                break
    if len(out) != entry.size:
        raise PackFormatError(
            f"object {entry.index} inflates to {len(out)} bytes but "
            f"declares {entry.size}")
    return bytes(out)
