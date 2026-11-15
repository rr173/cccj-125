"""Hand-written expectations for the real handoff demo.

Everything below is written from the demo narrative -- base commit, a data
branch that adds one daily feed, a layout branch that edits one copybook,
and a merge -- not from the reader's output.  Commit ids are resolved at
run time from the repository build log, but parent relationships, paths,
change kinds and blob bytes are literals.
"""

from __future__ import annotations

# The decoder returns COMP-3 amounts as ``decimal.Decimal`` and 9(8) dates
# as integers; JSON export stringifies them.  These are the native values
# the demo asserts against, written as ``Decimal``/``int`` literals.
from decimal import Decimal

# Paths in the demo repository (all UTF-8, so b64 is a plain encoding).
BASE_FILES = [
    "examples/copybooks/ACCT-1.cpy",
    "examples/copybooks/ACCT-2.cpy",
    "examples/data/BANKA-20240315.dat",
    "examples/data/BANKA-20241015.dat",
    "examples/manifest.json",
]

DATA_BRANCH_ADDED = ["examples/data/BANKA-20240615.dat"]
LAYOUT_BRANCH_CHANGED = ["examples/copybooks/ACCT-1.cpy"]

#: (branch label, list of (change, path)) relative to the base
BRANCH_DIFFS = {
    "data": [(("added"), "examples/data/BANKA-20240615.dat")],
    "layout": [(("modified"), "examples/copybooks/ACCT-1.cpy")],
}

#: First merge: ``merge: data`` has base and the data tip as parents.
MERGE1_PARENT_DIFFS = {
    0: [("added", "examples/data/BANKA-20240615.dat")],
    1: [],
}

#: Second merge: ``merge: layout+data`` has merge1 and the layout tip.
MERGE2_PARENT_DIFFS = {
    0: [("modified", "examples/copybooks/ACCT-1.cpy")],
    1: [("added", "examples/data/BANKA-20240615.dat")],
}

#: Both branches were committed with *earlier* timestamps than their
#: parent; the reader must still put the base first.
CHILD_TIMESTAMPS_INVERTED = True

#: Decoding checks for the exported 2024-06-15 feed (layout v1).
EXPORTED_FEED = {
    "path": "examples/data/BANKA-20240615.dat",
    "line_count": 3,
    "records": [
        {"line": 1, "fields": {"ACCT-ID": "0000000010",
                               "ACCT-NAME": "DAVE DAVIS",
                               "BALANCE": Decimal("1111.11"),
                               "LAST-TXN-DATE": 20240614,
                               "LEGACY-CODE": "AB12"}},
        {"line": 2, "fields": {"ACCT-ID": "0000000011",
                               "ACCT-NAME": "EVE EVANS",
                               "BALANCE": Decimal("-22.22"),
                               "LAST-TXN-DATE": 20240615,
                               "LEGACY-CODE": "CD34"}},
        {"line": 3, "fields": {"ACCT-ID": "0000000012",
                               "ACCT-NAME": "FRANK FOSTER",
                               "BALANCE": Decimal("333333.33"),
                               "LAST-TXN-DATE": 20240615,
                               "LEGACY-CODE": "EF56"}},
    ],
    # 10 + 20 + 5 + 8 + 4 + 3 bytes; lines 1..2 are followed by "\n".
    "byte_ranges": [(0, 50), (51, 101), (102, 152)],
}

#: The copybook edit keeps the record length at 50 bytes.
LAYOUT_STILL_50_BYTES = True
