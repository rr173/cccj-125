"""Independent acceptance and layer oracle, deliberately separate.

This module re-derives the expected admission result from the contract and
the raw JSONL inputs without importing any production decision code:

* the accepted set is the union of every subset of the *locally valid*
  rows (enumerated across all tables on small handoff samples) that
  satisfies all real references internally -- which is exactly the
  greatest fixed point the engine must deliver;
* rejection layers are recomputed independently by a plain synchronous
  round loop using the "initial candidate" definition (locally rejected
  rows with a readable target key still count).

Only JSON parsing is shared with the package under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _path_get(fields: Any, parts: list[str]) -> Any:
    cur = fields
    for piece in parts:
        if isinstance(cur, dict):
            if piece not in cur:
                return None
            cur = cur[piece]
        elif isinstance(cur, list):
            try:
                idx = int(piece)
            except ValueError:
                return None
            if not 0 <= idx < len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class Row:
    table: str
    row_id: str
    fields: dict
    local_bad: bool
    # reference name -> ("skip",) | ("need", target_table, key, tuple|None)
    needs: dict


class Oracle:
    def __init__(self, contract: dict):
        self.contract = contract
        self.tables: dict[str, dict] = {t["name"]: t for t in
                                        contract["tables"]}
        self.rows: dict[str, list[Row]] = {}
        self._load()

    def _load(self) -> None:
        for t in self.contract["tables"]:
            rows: list[Row] = []
            for file_name in t["files"]:
                for obj in _read_jsonl(file_name):
                    rows.append(self._row(t, obj))
            self.rows[t["name"]] = rows

    def _row(self, t: dict, obj: dict) -> Row:
        fields = obj["fields"]
        rid = _path_get(fields, t["row_id_path"])
        local_bad = False
        for col, decl in t["fields"].items():
            v = _path_get(fields, decl["path"])
            if v is None:
                if not decl.get("nullable", False):
                    local_bad = True
            elif decl["type"] == "integer":
                if not _is_int(v):
                    local_bad = True
            else:
                if not isinstance(v, str):
                    local_bad = True
        needs: dict[str, Any] = {}
        for ref in t.get("references", []):
            vals = [_path_get(fields, t["fields"][c]["path"])
                    for c in ref["columns"]]
            nulls = [v is None for v in vals]
            if ref["rule"] == "optional" and all(nulls):
                needs[ref["name"]] = ("skip",)
                continue
            if ref["rule"] == "optional" and any(nulls):
                local_bad = True
                continue
            if any(nulls):
                # mandatory null: cannot match any non-null key tuple.
                needs[ref["name"]] = (
                    "need", ref["target_table"], ref["target_key"], None)
                continue
            types = tuple(t["fields"][c]["type"] for c in ref["columns"])
            needs[ref["name"]] = (
                "need", ref["target_table"], ref["target_key"],
                tuple(("i", v) if ty == "integer" else ("s", v)
                      for v, ty in zip(vals, types)))
        return Row(t["name"], rid, fields, local_bad, needs)

    def _key_index(self) -> dict:
        """Readable key tuples of every row, including local-reject ones."""
        index: dict[tuple, dict[tuple, set[str]]] = {}
        for tname, t in self.tables.items():
            for key_name, cols in t.get("candidate_keys", {}).items():
                types = tuple(t["fields"][c]["type"] for c in cols)
                for row in self.rows[tname]:
                    vals = [_path_get(row.fields, t["fields"][c]["path"])
                            for c in cols]
                    if any(v is None for v in vals):
                        continue
                    if any(ty == "integer" and not _is_int(v)
                           for v, ty in zip(vals, types)):
                        continue
                    if any(ty == "string" and not isinstance(v, str)
                           for v, ty in zip(vals, types)):
                        continue
                    tup = tuple(("i", v) if ty == "integer" else ("s", v)
                                for v, ty in zip(vals, types))
                    index.setdefault((tname, key_name), {}).setdefault(
                        tup, set()).add(row.row_id)
        return index

    def _all_valid_rows(self) -> list[Row]:
        return [r for rows in self.rows.values() for r in rows
                if not r.local_bad]

    def expected_kept(self) -> dict[str, set[str]]:
        """Brute-force union of every internally satisfying subset.

        A subset satisfies the references when every real reference of
        every chosen row resolves to at least one chosen parent (so closed
        cycles are admissible and anchor reachability plays no role).  The
        union of all such subsets is the greatest supported set.
        """
        valid = self._all_valid_rows()
        by_id = {(r.table, r.row_id): r for r in valid}
        index = self._key_index()
        kept: set[tuple[str, str]] = set()
        # 2**n over the locally valid rows; only used for small samples.
        for bits in range(1 << len(valid)):
            chosen = {ident for i, ident in enumerate(by_id)
                      if bits & (1 << i)}
            ok = True
            for ident in chosen:
                row = by_id[ident]
                for need in row.needs.values():
                    if need[0] == "skip":
                        continue
                    _, tt, tk, tup = need
                    if tup is None:
                        ok = False
                        break
                    parents = index.get((tt, tk), {}).get(tup, set())
                    if not any((tt, pid) in chosen for pid in parents):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                kept.update(chosen)
        return {t: {rid for (tt, rid) in kept if tt == t}
                for t in self.tables}

    def expected_layers(self) -> dict[tuple[str, str], Optional[int]]:
        """Independent synchronous-round layer assignment."""
        index = self._key_index()
        status: dict[tuple[str, str], str] = {}
        layer: dict[tuple[str, str], Optional[int]] = {}
        for tname, rows in self.rows.items():
            for r in rows:
                ident = (tname, r.row_id)
                if r.local_bad:
                    status[ident] = "local"
                    layer[ident] = 0
                else:
                    status[ident] = "alive"
                    layer[ident] = None
        round_no = 0
        while True:
            round_no += 1
            doomed = []
            for ident, st in list(status.items()):
                if st != "alive":
                    continue
                tname, rid = ident
                row = next(r for r in self.rows[tname] if r.row_id == rid)
                for need in row.needs.values():
                    if need[0] == "skip":
                        continue
                    _, tt, tk, tup = need
                    parents = (index.get((tt, tk), {}).get(tup, set())
                               if tup is not None else set())
                    if not any(status[(tt, p)] == "alive"
                               for p in parents):
                        doomed.append(ident)
                        break
            if not doomed:
                break
            for ident in doomed:
                status[ident] = "dead"
                layer[ident] = round_no
        return layer
