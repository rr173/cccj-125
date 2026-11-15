"""Parsed, schema-validated correction order.

The order is UTF-8 JSON with three top-level members:

* ``source_sha256`` -- the full SHA-256 the original binary must have;
* ``layouts`` -- a named catalog.  Each entry names the Copybook path,
  layout name/version and codec (only ``cp037`` and ``cp500`` are
  supported).  One original may mix layouts;
* ``records`` -- an order-insensitive list of record coordinates.  Each
  record gives the half-open ``[byte_start, byte_end)`` span measured
  from the start of the file, the catalog entry to use, the SHA-256 of
  exactly that record span, and its ``edits``.

This module performs structural and coordinate validation only.  Path
resolution, value encoding, per-byte conflict checking and every digest
comparison happen in :mod:`copybook.rewrite.engine` against the actual
original bytes -- declared digests are never trusted as computed values.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..model import Layout
from ..parser import parse_copybook
from .errors import RewriteOrderError
from .strictjson import load_strict_json

SUPPORTED_CODECS = ("cp037", "cp500")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def is_plain_int(value) -> bool:
    """JSON coordinate/index check: bool is intentionally not an int."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_sha256_hex(value) -> bool:
    return isinstance(value, str) and _SHA256_RE.match(value) is not None


@dataclass
class EditSpec:
    raw_path: list
    expected_hex: str            # exactly the field's original bytes
    value: object


@dataclass
class RecordSpec:
    byte_start: int
    byte_end: int
    layout_key: str
    record_sha256: str
    edits: list[EditSpec] = dc_field(default_factory=list)

    @property
    def length(self) -> int:
        return self.byte_end - self.byte_start


@dataclass
class LayoutEntry:
    key: str
    copybook_path: Path
    name: str
    version: str
    codec: str
    layout: Layout
    copybook_sha256: str


@dataclass
class CorrectionOrder:
    source_sha256: str
    layouts: dict[str, LayoutEntry]
    records: list[RecordSpec]


def load_order(path, *, copybook_base: Path | None = None) -> CorrectionOrder:
    """Load and structurally validate a correction order document."""
    data = load_strict_json(path)
    if not isinstance(data, dict):
        raise RewriteOrderError("correction order top level must be an object")

    for required in ("source_sha256", "layouts", "records"):
        if required not in data:
            raise RewriteOrderError(
                f"correction order is missing required member {required!r}")
    source_sha256 = data["source_sha256"]
    if not is_sha256_hex(source_sha256):
        raise RewriteOrderError(
            "source_sha256 must be 64 lower-case hexadecimal characters")

    base = Path(copybook_base) if copybook_base is not None \
        else Path(path).resolve().parent
    layouts = _parse_layouts(data["layouts"], base)
    records = _parse_records(data["records"], layouts)
    return CorrectionOrder(source_sha256=source_sha256,
                           layouts=layouts, records=records)


# ------------------------------------------------------------------ layouts

def _parse_layouts(raw, base: Path) -> dict[str, LayoutEntry]:
    if not isinstance(raw, dict) or not raw:
        raise RewriteOrderError(
            "layouts must be a non-empty object of named catalog entries")
    entries: dict[str, LayoutEntry] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not key:
            raise RewriteOrderError("layout catalog keys must be strings")
        if not isinstance(item, dict):
            raise RewriteOrderError(f"layouts[{key!r}] must be an object")
        for required in ("copybook", "name", "version", "codec"):
            if required not in item:
                raise RewriteOrderError(
                    f"layouts[{key!r}] is missing required member "
                    f"{required!r}")
        copybook_value = item["copybook"]
        name = item["name"]
        version = item["version"]
        codec = item["codec"]
        if not all(isinstance(v, str) and v
                   for v in (copybook_value, name, version, codec)):
            raise RewriteOrderError(
                f"layouts[{key!r}]: copybook/name/version/codec must be "
                "non-empty strings")
        if codec not in SUPPORTED_CODECS:
            raise RewriteOrderError(
                f"layouts[{key!r}]: codec {codec!r} is not supported; "
                f"expected one of {', '.join(SUPPORTED_CODECS)}")
        cpy_path = Path(copybook_value)
        if not cpy_path.is_absolute():
            cpy_path = base / cpy_path
        if not cpy_path.is_file():
            raise RewriteOrderError(
                f"layouts[{key!r}]: copybook file not found: {cpy_path}")
        try:
            text = cpy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RewriteOrderError(
                f"layouts[{key!r}]: cannot read copybook {cpy_path}: {exc}")
        layout = parse_copybook(text, name=name, version=version, codec=codec)
        entries[key] = LayoutEntry(
            key=key, copybook_path=cpy_path, name=name, version=version,
            codec=codec, layout=layout,
            copybook_sha256=hashlib.sha256(
                cpy_path.read_bytes()).hexdigest())
    return entries


# ------------------------------------------------------------------ records

def _parse_records(raw, layouts: dict[str, LayoutEntry]) -> list[RecordSpec]:
    if not isinstance(raw, list):
        raise RewriteOrderError("records must be a list (it may be empty)")
    records: list[RecordSpec] = []
    for idx, item in enumerate(raw):
        where = f"records[{idx}]"
        if not isinstance(item, dict):
            raise RewriteOrderError(f"{where} must be an object")
        for required in ("byte_start", "byte_end", "layout",
                         "record_sha256"):
            if required not in item:
                raise RewriteOrderError(
                    f"{where} is missing required member {required!r}")
        start = item["byte_start"]
        end = item["byte_end"]
        if not is_plain_int(start) or not is_plain_int(end):
            raise RewriteOrderError(
                f"{where}: byte_start/byte_end must be non-negative "
                "integers (booleans are not accepted)")
        if start < 0 or end < 0:
            raise RewriteOrderError(f"{where}: coordinates must be >= 0")
        if end <= start:
            raise RewriteOrderError(
                f"{where}: byte_end ({end}) must be greater than "
                f"byte_start ({start})")
        layout_key = item["layout"]
        if not isinstance(layout_key, str) or layout_key not in layouts:
            raise RewriteOrderError(
                f"{where}: layout {layout_key!r} is not a known catalog "
                "entry")
        if end - start != layouts[layout_key].layout.record_length:
            raise RewriteOrderError(
                f"{where}: span length {end - start} does not match the "
                f"record_length "
                f"{layouts[layout_key].layout.record_length} of layout "
                f"{layout_key!r}")
        digest = item["record_sha256"]
        if not is_sha256_hex(digest):
            raise RewriteOrderError(
                f"{where}: record_sha256 must be 64 lower-case hex "
                "characters")
        raw_edits = item.get("edits", [])
        if "edits" in item and not isinstance(raw_edits, list):
            raise RewriteOrderError(f"{where}: edits must be a list")
        edits = [_parse_edit(raw_edits[j], f"{where}.edits[{j}]")
                 for j in range(len(raw_edits))]
        records.append(RecordSpec(
            byte_start=start, byte_end=end, layout_key=layout_key,
            record_sha256=digest, edits=edits))

    records.sort(key=lambda r: (r.byte_start, r.byte_end))
    for prev, cur in zip(records, records[1:]):
        if cur.byte_start < prev.byte_end:
            raise RewriteOrderError(
                f"record spans overlap or repeat: "
                f"[{prev.byte_start}, {prev.byte_end}) and "
                f"[{cur.byte_start}, {cur.byte_end})")
    return records


def _parse_edit(item, where: str) -> EditSpec:
    if not isinstance(item, dict):
        raise RewriteOrderError(f"{where} must be an object")
    for required in ("path", "expected_hex", "value"):
        if required not in item:
            raise RewriteOrderError(
                f"{where} is missing required member {required!r}")
    path = item["path"]
    if not isinstance(path, list) or not path:
        raise RewriteOrderError(
            f"{where}: path must be a non-empty array")
    # A path starts at a child of the record root group, so its first
    # component is always a field name, never an OCCURS index.
    if not isinstance(path[0], str):
        raise RewriteOrderError(
            f"{where}: the first path component must be a field name "
            f"string, got {path[0]!r}")
    for component in path:
        if isinstance(component, bool) or not (
                isinstance(component, str) or is_plain_int(component)):
            raise RewriteOrderError(
                f"{where}: path components must be upper-case field name "
                f"strings or non-negative integer indices, got "
                f"{component!r}")
        if is_plain_int(component) and component < 0:
            raise RewriteOrderError(
                f"{where}: path indices must be non-negative, got "
                f"{component}")
    expected_hex = item["expected_hex"]
    if not isinstance(expected_hex, str) or not expected_hex \
            or any(c not in "0123456789abcdefABCDEF"
                   for c in expected_hex) or len(expected_hex) % 2:
        raise RewriteOrderError(
            f"{where}: expected_hex must be an even-length hex string")
    return EditSpec(raw_path=path, expected_hex=expected_hex,
                    value=item["value"])
