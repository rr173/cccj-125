"""Configuration for batch handoff auditing.

The auditor never decodes raw fields itself and never extends the copybook
grammar.  It only walks the ``fields`` mapping already produced by
:meth:`DecodeResult.write_jsonl` using *field paths*: arrays of string keys
(or numeric indices for OCCURS lists).

Quantity arithmetic is exact: signed decimal strings with at most three
fractional digits are converted to integer *thousandth units* (milli-units),
so no ``float`` is ever involved and trailing decimal digits are preserved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .errors import AuditConfigError

# Optional sign, at least one integer digit, optional dot with 1..3 digits.
# No exponent, no whitespace, no bare ".5", no thousands separators.
_QUANTITY_RE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]{1,3})?$")
_COUNT_RE = re.compile(r"^[+-]?[0-9]+$")
MILLI_SCALE = 3


@dataclass(frozen=True)
class FieldPath:
    """A string-array path into a decoded record's ``fields`` mapping.

    Numeric parts address OCCURS lists, e.g. ``("TOTALS", "0", "QTY")``.
    """

    parts: tuple[str, ...]

    @classmethod
    def parse(cls, spec: Any) -> "FieldPath":
        if not isinstance(spec, list) or not spec:
            raise AuditConfigError(
                f"field path must be a non-empty array of strings, got {spec!r}")
        parts: list[str] = []
        for piece in spec:
            if not isinstance(piece, str) or not piece:
                raise AuditConfigError(
                    f"field path segments must be non-empty strings, got {piece!r} "
                    f"in {spec!r}")
            parts.append(piece)
        return cls(tuple(parts))

    def get(self, fields: Any) -> Any:
        """Return the value at this path, or ``None`` if any hop is missing.

        A present ``None`` value and a missing path are indistinguishable to
        the auditor; both mean "the business item is not available".
        """
        current = fields
        for piece in self.parts:
            if isinstance(current, dict):
                if piece not in current:
                    return None
                current = current[piece]
            elif isinstance(current, list):
                try:
                    idx = int(piece)
                except ValueError:
                    return None
                if not 0 <= idx < len(current):
                    return None
                current = current[idx]
            else:
                return None
        return current

    def as_text(self, fields: Any) -> Optional[str]:
        value = self.get(fields)
        if value is None:
            return None
        return str(value)


def parse_quantity_milli(raw: Optional[str]) -> Optional[int]:
    """Convert a signed decimal string (max 3 fractional digits) to milli-units.

    Returns ``None`` when *raw* is not a legal quantity string; callers are
    expected to record an invalid-quantity diagnostic in that case.
    """
    if raw is None or not _QUANTITY_RE.match(raw):
        return None
    sign = -1 if raw.startswith("-") else 1
    body = raw[1:] if raw[:1] in ("+", "-") else raw
    if "." in body:
        whole, frac = body.split(".", 1)
    else:
        whole, frac = body, ""
    frac = frac.ljust(MILLI_SCALE, "0")
    return sign * (int(whole) * 1000 + int(frac))


def parse_count(raw: Optional[str]) -> Optional[int]:
    """Parse a declared row count (non-negative integer string)."""
    if raw is None or not _COUNT_RE.match(raw):
        return None
    value = int(raw)
    return value if value >= 0 else None


def format_milli(value: int) -> str:
    """Render integer milli-units back as an exact decimal string."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, frac = divmod(value, 1000)
    return f"{sign}{whole}.{frac:03d}"


@dataclass(frozen=True)
class AuditConfig:
    """Field mapping plus the fixed, finite category table.

    ``btail_total_paths`` maps every allowed category to the path of its
    declared total on a BTAIL record.  It must cover exactly the category
    table -- no silent zero-fill for categories the scheme forgot.
    """

    categories: tuple[str, ...]
    record_type_path: FieldPath
    file_id_path: FieldPath
    batch_id_path: FieldPath
    seq_path: FieldPath
    category_path: FieldPath
    quantity_path: FieldPath
    btail_detail_count_path: FieldPath
    btail_total_paths: dict[str, FieldPath]
    ftail_batch_count_path: FieldPath
    ftail_detail_count_path: FieldPath

    @classmethod
    def load(cls, path: Union[str, Path]) -> "AuditConfig":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuditConfigError(f"cannot read audit config {p}: {exc}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "AuditConfig":
        if not isinstance(data, dict):
            raise AuditConfigError("audit config must be a JSON object")
        cats_raw = data.get("categories")
        if not isinstance(cats_raw, list) or not cats_raw:
            raise AuditConfigError(
                "'categories' must be a non-empty array of category codes")
        categories: list[str] = []
        for cat in cats_raw:
            if not isinstance(cat, str) or not cat:
                raise AuditConfigError(f"invalid category code: {cat!r}")
            if cat in categories:
                raise AuditConfigError(f"duplicate category code: {cat!r}")
            categories.append(cat)

        paths_raw = data.get("paths")
        if not isinstance(paths_raw, dict):
            raise AuditConfigError("config requires a 'paths' object")

        def need(key: str) -> FieldPath:
            if key not in paths_raw:
                raise AuditConfigError(f"paths.{key} is required")
            return FieldPath.parse(paths_raw[key])

        totals_raw = paths_raw.get("btail_totals")
        if not isinstance(totals_raw, dict) or not totals_raw:
            raise AuditConfigError(
                "paths.btail_totals must be an object mapping each category "
                "to a field path")
        if set(totals_raw) != set(categories):
            raise AuditConfigError(
                "paths.btail_totals must cover exactly the categories "
                f"{categories}; got {sorted(totals_raw)}")
        btail_total_paths = {
            cat: FieldPath.parse(spec) for cat, spec in totals_raw.items()}

        return cls(
            categories=tuple(categories),
            record_type_path=need("record_type"),
            file_id_path=need("file_id"),
            batch_id_path=need("batch_id"),
            seq_path=need("seq"),
            category_path=need("category"),
            quantity_path=need("quantity"),
            btail_detail_count_path=need("btail_detail_count"),
            btail_total_paths=btail_total_paths,
            ftail_batch_count_path=need("ftail_batch_count"),
            ftail_detail_count_path=need("ftail_detail_count"),
        )
