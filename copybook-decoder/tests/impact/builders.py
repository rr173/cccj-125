"""Shared builders for impact-report tests: temp manifests + copybooks."""

import json
from pathlib import Path


def write_copybook(directory: Path, name: str, text: str) -> None:
    cpy_dir = directory / "copybooks"
    cpy_dir.mkdir(parents=True, exist_ok=True)
    (cpy_dir / name).write_text(text, encoding="utf-8")


def write_manifest(directory: Path, layouts: list) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(json.dumps({"layouts": layouts}), encoding="utf-8")
    return path


def entry(sender="BANKA", copybook="ACCT", version="1",
          effective_from="2024-01-01", effective_to=None, file=None,
          codec="cp037"):
    e = {"sender": sender, "copybook": copybook, "version": version,
         "effective_from": effective_from,
         "file": file or f"copybooks/{copybook}-{version}.cpy",
         "codec": codec}
    if effective_to is not None:
        e["effective_to"] = effective_to
    return e


# The canonical "before" layout used by engine tests (50 bytes).
OLD_V1 = """      01  ACCT-RECORD.
          05  ACCT-ID         PIC X(10).
          05  ACCT-NAME       PIC X(20).
          05  BALANCE         PIC S9(7)V99 COMP-3.
          05  LAST-TXN-DATE   PIC 9(8).
          05  LEGACY-CODE     PIC X(4).
          05  LEGACY-CODE-NUM REDEFINES LEGACY-CODE PIC 9(4).
          05  OLD-STATUS      PIC X(2).
          05  FILLER          PIC X(1).
"""

# The canonical "after" layout (51 bytes): BALANCE rescaled at constant
# width, new view BALANCE-TEXT, CURRENCY inserted, tail fields shifted,
# OLD-STATUS removed.
NEW_V2 = """      01  ACCT-RECORD.
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


def build_main_scenario(base: Path):
    """Old: BANKA+BANKB on ACCT v1.  New: BANKA switches to v2 from
    2024-09-01, BANKB stays on v1.  Returns (old_manifest, new_manifest)."""
    old_dir = base / "old"
    new_dir = base / "new"
    write_copybook(old_dir, "ACCT-1.cpy", OLD_V1)
    write_copybook(new_dir, "ACCT-1.cpy", OLD_V1)
    write_copybook(new_dir, "ACCT-2.cpy", NEW_V2)
    old_manifest = write_manifest(old_dir, [
        entry(sender="BANKA"),
        entry(sender="BANKB"),
    ])
    new_manifest = write_manifest(new_dir, [
        entry(sender="BANKA", effective_to="2024-08-31"),
        entry(sender="BANKA", version="2", effective_from="2024-09-01"),
        entry(sender="BANKB"),
    ])
    return old_manifest, new_manifest
