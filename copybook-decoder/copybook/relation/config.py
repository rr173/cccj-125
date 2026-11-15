"""The reference contract: tables, columns, candidate keys and references.

A contract is plain declarative JSON.  It never contains executable text:
table names, column names and string key values may include dots, quotes
or separator characters, and they are always carried as data (bound
parameters / tuple components), never concatenated into SQL or joined
into a fake tuple string.

Contract shape::

    {
      "tables": [
        {
          "name": "accounts",
          "files": ["a.jsonl", "b.jsonl"],
          "row_id_path": ["ID"],
          "fields": {
            "acct_id": {"path": ["ACCT", "ID"], "type": "string"},
            "tier":    {"path": ["TIER"], "type": "integer",
                        "nullable": true}
          },
          "candidate_keys": {"pk_acct": ["acct_id"]},
          "references": [
            {"name": "home_branch", "columns": ["br"],
             "target_table": "branches", "target_key": "pk_branch",
             "rule": "mandatory"},
            {"name": "parent_acct", "columns": ["p1", "p2"],
             "target_table": "accounts", "target_key": "pk_pair",
             "rule": "optional"}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .errors import RelationConfigError

STRING = "string"
INTEGER = "integer"
TYPES = (STRING, INTEGER)

OPTIONAL = "optional"
MANDATORY = "mandatory"
RULES = (OPTIONAL, MANDATORY)

#: Reference rule values as they appear in the contract.
CANDIDATE = "candidate"


@dataclass(frozen=True)
class FieldPath:
    """A non-empty string-array path into a record's ``fields`` mapping.

    Numeric string parts may address OCCURS lists exactly as in the audit,
    mask and settle packages; the pieces themselves stay opaque strings,
    so a key containing dots or quotes is one single key.
    """

    parts: tuple[str, ...]

    @classmethod
    def parse(cls, spec: Any, *, what: str = "field path") -> "FieldPath":
        if not isinstance(spec, list) or not spec:
            raise RelationConfigError(
                f"{what} must be a non-empty array of strings, got {spec!r}")
        parts: list[str] = []
        for piece in spec:
            if not isinstance(piece, str) or not piece:
                raise RelationConfigError(
                    f"{what} segments must be non-empty strings, got "
                    f"{piece!r} in {spec!r}")
            parts.append(piece)
        return cls(tuple(parts))

    def get(self, fields: Any) -> Any:
        """Value at the path, or ``None`` when any hop is missing.

        An explicit JSON ``null`` and a missing hop are both returned as
        ``None``; the local row validator decides whether null is legal.
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

    def spec(self) -> list[str]:
        return list(self.parts)


@dataclass(frozen=True)
class FieldDecl:
    """One declared logical column."""

    name: str
    path: FieldPath
    type: str
    nullable: bool

    def spec(self) -> dict:
        return {"path": self.path.spec(), "type": self.type,
                "nullable": self.nullable}


@dataclass(frozen=True)
class Reference:
    """One named reference from child columns to a target candidate key."""

    name: str
    columns: tuple[str, ...]
    target_table: str
    target_key: str
    rule: str                       # OPTIONAL or MANDATORY

    @property
    def optional(self) -> bool:
        return self.rule == OPTIONAL

    def spec(self) -> dict:
        return {"name": self.name, "columns": list(self.columns),
                "target_table": self.target_table,
                "target_key": self.target_key, "rule": self.rule}


@dataclass(frozen=True)
class TableContract:
    """One logical table: identity, columns, keys and outgoing references."""

    ordinal: int
    name: str
    files: tuple[str, ...]
    row_id_path: FieldPath
    fields: dict[str, FieldDecl]
    candidate_keys: dict[str, tuple[str, ...]]
    references: tuple[Reference, ...]

    def reference(self, name: str) -> Optional[Reference]:
        for ref in self.references:
            if ref.name == name:
                return ref
        return None


@dataclass(frozen=True)
class RelationContract:
    """The whole validated handoff contract."""

    tables: dict[str, TableContract]
    table_order: tuple[str, ...]

    @classmethod
    def load(cls, path: Union[str, Path]) -> "RelationContract":
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise RelationConfigError(
                f"cannot read reference contract {p}: {exc}")
        try:
            data = json_loads_strict(text, str(p))
        except json.JSONDecodeError as exc:
            raise RelationConfigError(
                f"reference contract {p} is not valid JSON: {exc}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "RelationContract":
        if not isinstance(data, dict):
            raise RelationConfigError(
                "reference contract must be a JSON object")
        tables_raw = data.get("tables")
        if not isinstance(tables_raw, list) or not tables_raw:
            raise RelationConfigError(
                "contract requires a non-empty 'tables' array")
        tables: dict[str, TableContract] = {}
        order: list[str] = []
        for ordinal, raw in enumerate(tables_raw):
            table = _parse_table(raw, ordinal, known=tables)
            if table.name in tables:
                raise RelationConfigError(
                    f"duplicate table name {table.name!r}")
            tables[table.name] = table
            order.append(table.name)
        # Cross-table references are only checkable once every table is
        # parsed; do a second, purely declarative pass.
        for table in tables.values():
            for ref in table.references:
                target = tables.get(ref.target_table)
                if target is None:
                    raise RelationConfigError(
                        f"table {table.name!r} reference {ref.name!r} "
                        f"targets unknown table {ref.target_table!r}")
                key_cols = target.candidate_keys.get(ref.target_key)
                if key_cols is None:
                    raise RelationConfigError(
                        f"table {table.name!r} reference {ref.name!r} "
                        f"targets unknown candidate key {ref.target_key!r} "
                        f"on table {ref.target_table!r}")
                if len(key_cols) != len(ref.columns):
                    raise RelationConfigError(
                        f"reference {table.name!r}.{ref.name!r} has "
                        f"{len(ref.columns)} column(s) but target key "
                        f"{ref.target_key!r} has {len(key_cols)}")
                for child_col, target_col in zip(ref.columns, key_cols):
                    child_type = table.fields[child_col].type
                    target_type = target.fields[target_col].type
                    if child_type != target_type:
                        raise RelationConfigError(
                            f"reference {table.name!r}.{ref.name!r} column "
                            f"{child_col!r} ({child_type}) does not match "
                            f"target column {target_col!r} ({target_type}) "
                            f"on {ref.target_table!r}.{ref.target_key!r}; "
                            "matching never converts types")
        return cls(tables=tables, table_order=tuple(order))


def _parse_table(raw: Any, ordinal: int,
                 *, known: dict[str, TableContract]) -> TableContract:
    where = f"table at position {ordinal}"
    if not isinstance(raw, dict):
        raise RelationConfigError(f"{where} must be a JSON object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise RelationConfigError(
            f"{where}: 'name' must be a non-empty string")
    where = f"table {name!r}"

    files_raw = raw.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise RelationConfigError(
            f"{where}: 'files' must be a non-empty array of file paths")
    files: list[str] = []
    for item in files_raw:
        if not isinstance(item, str) or not item:
            raise RelationConfigError(
                f"{where}: every entry of 'files' must be a non-empty "
                f"string, got {item!r}")
        if item in files:
            raise RelationConfigError(
                f"{where}: file {item!r} is listed more than once")
        files.append(item)

    row_id_path = FieldPath.parse(
        raw.get("row_id_path"), what=f"{where}.row_id_path")

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, dict) or not fields_raw:
        raise RelationConfigError(
            f"{where}: 'fields' must be a non-empty object of declared "
            "columns")
    fields: dict[str, FieldDecl] = {}
    for col_name, spec_raw in fields_raw.items():
        if not isinstance(col_name, str) or not col_name:
            raise RelationConfigError(
                f"{where}: field names must be non-empty strings, got "
                f"{col_name!r}")
        if not isinstance(spec_raw, dict):
            raise RelationConfigError(
                f"{where}: field {col_name!r} must be an object")
        col_type = spec_raw.get("type")
        if col_type not in TYPES:
            raise RelationConfigError(
                f"{where}: field {col_name!r} type must be one of "
                f"{', '.join(TYPES)}, got {col_type!r}")
        path = FieldPath.parse(
            spec_raw.get("path"), what=f"{where}.fields.{col_name}.path")
        nullable = spec_raw.get("nullable", False)
        if not isinstance(nullable, bool):
            raise RelationConfigError(
                f"{where}: field {col_name!r}.nullable must be a boolean")
        fields[col_name] = FieldDecl(
            name=col_name, path=path, type=col_type, nullable=nullable)

    keys_raw = raw.get("candidate_keys", {})
    if not isinstance(keys_raw, dict):
        raise RelationConfigError(
            f"{where}: 'candidate_keys' must be an object")
    candidate_keys: dict[str, tuple[str, ...]] = {}
    for key_name, cols_raw in keys_raw.items():
        if not isinstance(key_name, str) or not key_name:
            raise RelationConfigError(
                f"{where}: candidate key names must be non-empty strings, "
                f"got {key_name!r}")
        if not isinstance(cols_raw, list) or not cols_raw:
            raise RelationConfigError(
                f"{where}: candidate key {key_name!r} must be a non-empty "
                "array of declared column names")
        cols: list[str] = []
        for col in cols_raw:
            if not isinstance(col, str) or not col:
                raise RelationConfigError(
                    f"{where}: candidate key {key_name!r} entries must be "
                    f"non-empty strings, got {col!r}")
            if col not in fields:
                raise RelationConfigError(
                    f"{where}: candidate key {key_name!r} uses undeclared "
                    f"column {col!r}")
            if fields[col].nullable:
                raise RelationConfigError(
                    f"{where}: candidate key {key_name!r} column {col!r} "
                    "is nullable; key columns must be non-nullable")
            if col in cols:
                raise RelationConfigError(
                    f"{where}: candidate key {key_name!r} lists column "
                    f"{col!r} more than once")
            cols.append(col)
        if key_name in candidate_keys:
            raise RelationConfigError(
                f"{where}: duplicate candidate key name {key_name!r}")
        candidate_keys[key_name] = tuple(cols)

    refs_raw = raw.get("references", [])
    if not isinstance(refs_raw, list):
        raise RelationConfigError(
            f"{where}: 'references' must be an array")
    references: list[Reference] = []
    seen_ref_names: set[str] = set()
    for ref_raw in refs_raw:
        if not isinstance(ref_raw, dict):
            raise RelationConfigError(
                f"{where}: every reference must be an object")
        ref_name = ref_raw.get("name")
        if not isinstance(ref_name, str) or not ref_name:
            raise RelationConfigError(
                f"{where}: reference 'name' must be a non-empty string")
        if ref_name in seen_ref_names:
            raise RelationConfigError(
                f"{where}: duplicate reference name {ref_name!r}")
        seen_ref_names.add(ref_name)
        cols_raw = ref_raw.get("columns")
        if not isinstance(cols_raw, list) or not cols_raw:
            raise RelationConfigError(
                f"{where}: reference {ref_name!r} needs a non-empty "
                "'columns' array")
        ref_cols: list[str] = []
        for col in cols_raw:
            if not isinstance(col, str) or not col:
                raise RelationConfigError(
                    f"{where}: reference {ref_name!r} columns must be "
                    f"non-empty strings, got {col!r}")
            if col not in fields:
                raise RelationConfigError(
                    f"{where}: reference {ref_name!r} uses undeclared "
                    f"column {col!r}")
            if col in ref_cols:
                raise RelationConfigError(
                    f"{where}: reference {ref_name!r} lists column "
                    f"{col!r} more than once")
            ref_cols.append(col)
        target_table = ref_raw.get("target_table")
        if not isinstance(target_table, str) or not target_table:
            raise RelationConfigError(
                f"{where}: reference {ref_name!r} needs a non-empty "
                "'target_table' string")
        target_key = ref_raw.get("target_key")
        if not isinstance(target_key, str) or not target_key:
            raise RelationConfigError(
                f"{where}: reference {ref_name!r} needs a non-empty "
                "'target_key' string")
        rule = ref_raw.get("rule")
        if rule not in RULES:
            raise RelationConfigError(
                f"{where}: reference {ref_name!r} rule must be one of "
                f"{', '.join(RULES)}, got {rule!r}")
        references.append(Reference(
            name=ref_name, columns=tuple(ref_cols),
            target_table=target_table, target_key=target_key, rule=rule))

    return TableContract(
        ordinal=ordinal, name=name, files=tuple(files),
        row_id_path=row_id_path, fields=fields,
        candidate_keys=candidate_keys, references=tuple(references))


# ----------------------------------------------------------------- helpers

def json_loads_strict(text: str, source: str) -> Any:
    """Parse JSON rejecting duplicate object members.

    Standard ``json.loads`` silently keeps the last value of a repeated
    member; a handoff record (or contract) with duplicated members is
    ambiguous and must be treated as a structural failure instead.
    """

    def reject_duplicates(pairs):
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise json.JSONDecodeError(
                    f"duplicate JSON object member {key!r}", text, 0)
            seen.add(key)
        return dict(pairs)

    return json.loads(text, object_pairs_hook=reject_duplicates)


def encode_tuple(values: list[Any], types: tuple[str, ...]) -> str:
    """Canonical text of a matched key/reference tuple.

    Every component is tagged with its declared type, so a string ``"7"``
    and an integer ``7`` can never compare equal, and integers use the
    canonical decimal spelling of the exact Python int (signed 128-bit and
    beyond are preserved).  Strings are never trimmed or case-folded.
    """
    tagged: list[list[str]] = []
    for value, col_type in zip(values, types):
        if col_type == INTEGER:
            if isinstance(value, bool):  # bool is an int subclass
                raise RelationConfigError(
                    "boolean is not an integer key component")
            tagged.append(["i", str(value)])
        else:
            tagged.append(["s", value])
    return json.dumps(tagged, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def decode_tuple(text: str) -> list[dict]:
    """Inverse of :func:`encode_tuple` for human-facing explanations."""
    raw = json.loads(text)
    return [{"type": "integer" if tag == "i" else "string",
             "value": int(value) if tag == "i" else value}
            for tag, value in raw]
