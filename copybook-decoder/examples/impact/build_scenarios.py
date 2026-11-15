#!/usr/bin/env python3
"""Build the hand-crafted impact-report scenarios.

Every scenario is a pair of receiving manifests (``old/`` = what runs in
production today, ``new/`` = the candidate that introduces a new copybook
version) plus the copybooks they reference.  No business data files are
involved: the impact report never reads any.

The layouts were designed on paper first; ``expected_results.py`` lists
the field differences, date ranges and conclusions derived by hand, and
``run_scenarios.py`` checks the program output against them.
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- layouts
# Field map of ACCT v1 (hand-computed, half-open byte ranges):
#   ACCT-ID        X(10)              [0, 10)
#   ACCT-NAME      X(20)              [10, 30)
#   BALANCE        S9(7)V99 COMP-3    [30, 35)   5 packed bytes
#   LAST-TXN-DATE  9(8)               [35, 43)
#   LEGACY-CODE    X(4)               [43, 47)
#   LEGACY-CODE-NUM REDEFINES view    [43, 47)   same bytes, no storage
#   OLD-STATUS     X(2)               [47, 49)
#   FILLER         X(1)               [49, 50)
# record length: 50
ACCT_V1 = """      * ACCT daily account feed -- layout version 1 (50 bytes)
       01  ACCT-RECORD.
           05  ACCT-ID         PIC X(10).
           05  ACCT-NAME       PIC X(20).
           05  BALANCE         PIC S9(7)V99 COMP-3.
           05  LAST-TXN-DATE   PIC 9(8).
           05  LEGACY-CODE     PIC X(4).
           05  LEGACY-CODE-NUM REDEFINES LEGACY-CODE PIC 9(4).
           05  OLD-STATUS      PIC X(2).
           05  FILLER          PIC X(1).
"""

# ACCT v2 (51 bytes):
#   * BALANCE rescaled S9(7)V99 -> S9(6)V999, still 5 packed bytes:
#     same position, different interpretation (reinterpreted);
#   * BALANCE-TEXT: a brand-new REDEFINES view over the BALANCE bytes;
#   * CURRENCY X(3) inserted in front of the tail fields, so
#     LAST-TXN-DATE / LEGACY-CODE / LEGACY-CODE-NUM shift by +3 (moved);
#   * OLD-STATUS removed (-2 bytes);
#   record length 50 -> 51 (+3 CURRENCY, -2 OLD-STATUS).
ACCT_V2 = """      * ACCT daily account feed -- layout version 2 (51 bytes)
       01  ACCT-RECORD.
           05  ACCT-ID         PIC X(10).
           05  ACCT-NAME       PIC X(20).
           05  BALANCE         PIC S9(6)V999 COMP-3.
           05  BALANCE-TEXT    REDEFINES BALANCE PIC X(5).
           05  CURRENCY        PIC X(3).
           05  LAST-TXN-DATE   PIC 9(8).
           05  LEGACY-CODE     PIC X(4).
           05  LEGACY-CODE-NUM REDEFINES LEGACY-CODE PIC 9(4).
           05  FILLER          PIC X(1).
"""

# CARD v1 -> v2: only additive changes inside former FILLER bytes plus a
# new REDEFINES view; record length stays 13 -> compatible.
CARD_V1 = """      * CARD feed -- layout version 1 (13 bytes)
       01  CARD-RECORD.
           05  CARD-ID   PIC X(6).
           05  AMOUNT    PIC S9(5)V99 COMP-3.
           05  FILLER    PIC X(3).
"""

CARD_V2 = """      * CARD feed -- layout version 2 (13 bytes)
       01  CARD-RECORD.
           05  CARD-ID   PIC X(6).
           05  AMOUNT    PIC S9(5)V99 COMP-3.
           05  AMT-TEXT  REDEFINES AMOUNT PIC X(4).
           05  REGION    PIC X(3).
"""

# GRID v1 -> v2: STATUS keeps its name and bytes but moves to a
# different group, so its full group path changes.
GRID_V1 = """      * GRID feed -- layout version 1 (6 bytes)
       01  GRID-RECORD.
           05  GRID-ID   PIC X(4).
           05  GRP-OLD.
               10  STATUS  PIC X(2).
"""

GRID_V2 = """      * GRID feed -- layout version 2 (6 bytes)
       01  GRID-RECORD.
           05  GRID-ID   PIC X(4).
           05  GRP-NEW.
               10  STATUS  PIC X(2).
"""

# Broken v2: GRP-A declared twice -> duplicate group path
# ACCT-RECORD/GRP-A and duplicate leaf path ACCT-RECORD/GRP-A/CODE;
# CODE also appears in GRP-B (same name, different group).
ACCT_V2_DUPPATH = """      * ACCT v2 draft with a duplicate group declaration
       01  ACCT-RECORD.
           05  ACCT-ID   PIC X(10).
           05  GRP-A.
               10  CODE  PIC X(2).
           05  GRP-A.
               10  CODE  PIC 9(2).
           05  GRP-B.
               10  CODE  PIC X(4).
"""


def _entry(sender, copybook, version, effective_from, effective_to=None,
           file=None):
    e = {"sender": sender, "copybook": copybook, "version": version,
         "effective_from": effective_from,
         "file": file or f"copybooks/{copybook}-{version}.cpy",
         "codec": "cp037"}
    if effective_to is not None:
        e["effective_to"] = effective_to
    return e


def _write_scenario(root: Path, name: str, old_layouts, new_layouts,
                    old_copybooks, new_copybooks) -> None:
    for side, layouts, copybooks in (("old", old_layouts, old_copybooks),
                                     ("new", new_layouts, new_copybooks)):
        base = root / name / side
        (base / "copybooks").mkdir(parents=True, exist_ok=True)
        (base / "manifest.json").write_text(
            json.dumps({"layouts": layouts}, indent=2) + "\n",
            encoding="utf-8")
        for cpy_name, text in copybooks.items():
            (base / "copybooks" / cpy_name).write_text(text,
                                                       encoding="utf-8")


def build_all(root: Path) -> list:
    """Write every scenario under *root*; returns the scenario names."""
    root.mkdir(parents=True, exist_ok=True)

    # 1. main: BANKA switches ACCT v1 -> v2 on 2024-09-01; BANKB stays
    #    on v1 for all dates (its effective range is untouched).
    _write_scenario(
        root, "main",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01"),
                     _entry("BANKB", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKA", "ACCT", "2", "2024-09-01"),
                     _entry("BANKB", "ACCT", "1", "2024-01-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1, "ACCT-2.cpy": ACCT_V2})

    # 2. compatible: CARD v2 only fills former FILLER bytes and adds a
    #    REDEFINES view; every existing field keeps its bytes.
    _write_scenario(
        root, "compatible",
        old_layouts=[_entry("BANKC", "CARD", "1", "2024-01-01")],
        new_layouts=[_entry("BANKC", "CARD", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKC", "CARD", "2", "2024-09-01")],
        old_copybooks={"CARD-1.cpy": CARD_V1},
        new_copybooks={"CARD-1.cpy": CARD_V1, "CARD-2.cpy": CARD_V2})

    # 3. regroup: STATUS moves to another group; the bare name is the
    #    same but the group path is not.
    _write_scenario(
        root, "regroup",
        old_layouts=[_entry("BANKD", "GRID", "1", "2024-01-01")],
        new_layouts=[_entry("BANKD", "GRID", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKD", "GRID", "2", "2024-09-01")],
        old_copybooks={"GRID-1.cpy": GRID_V1},
        new_copybooks={"GRID-1.cpy": GRID_V1, "GRID-2.cpy": GRID_V2})

    # 4. conflict: the new manifest overlaps v1 and v2 on
    #    2024-08-15..2024-08-31 (closed intervals).
    _write_scenario(
        root, "conflict",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKA", "ACCT", "2", "2024-08-15")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1, "ACCT-2.cpy": ACCT_V2})

    # 5. gap: the new manifest covers nothing in September 2024.
    _write_scenario(
        root, "gap",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKA", "ACCT", "2", "2024-10-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1, "ACCT-2.cpy": ACCT_V2})

    # 6. nobaseline: the new manifest onboards BANKC/CARD, which the old
    #    manifest never knew; BANKA stays on identical v1 throughout.
    _write_scenario(
        root, "nobaseline",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01"),
                     _entry("BANKC", "CARD", "1", "2024-01-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1, "CARD-1.cpy": CARD_V1})

    # 7. missing_copybook: v9 references a file that does not exist.
    _write_scenario(
        root, "missing_copybook",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKA", "ACCT", "9", "2024-09-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1})

    # 8. duplicate_path: the new v2 copybook declares GRP-A twice.
    _write_scenario(
        root, "duplicate_path",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01",
                            "2024-08-31"),
                     _entry("BANKA", "ACCT", "2", "2024-09-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V1,
                       "ACCT-2.cpy": ACCT_V2_DUPPATH})

    # 9. version_reused: both manifests say "version 1" for all dates,
    #    but the files behind that version differ.
    _write_scenario(
        root, "version_reused",
        old_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        new_layouts=[_entry("BANKA", "ACCT", "1", "2024-01-01")],
        old_copybooks={"ACCT-1.cpy": ACCT_V1},
        new_copybooks={"ACCT-1.cpy": ACCT_V2})

    return ["main", "compatible", "regroup", "conflict", "gap",
            "nobaseline", "missing_copybook", "duplicate_path",
            "version_reused"]


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    for scenario in build_all(here / "scenarios"):
        print("built", scenario)
