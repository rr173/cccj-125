#!/usr/bin/env python3
"""Hand-written expectations for the impact-report scenarios.

Every field difference, date range and conclusion below was derived by
hand from the two layout definitions and the manifest effective ranges
*before* the report program was run; ``run_scenarios.py`` asserts that
the program output matches this table.  Nothing here is computed from
program output.

Conventions
-----------
* byte ranges are half-open ``[start, end)``;
* effective date ranges are closed intervals; ``None`` = open-ended;
* exit code 0 = compatible, 1 = incompatible (report written),
  2 = unverifiable (problems prevent a complete comparison).
"""

# ---------------------------------------------------------------- main ----
# Hand derivation for BANKA/ACCT v1 -> v2 (affected 2024-09-01..open):
#
#   path                            old bytes  new bytes  verdict
#   ACCT-RECORD/ACCT-ID             [0,10)     [0,10)     unchanged
#   ACCT-RECORD/ACCT-NAME           [10,30)    [10,30)    unchanged
#   ACCT-RECORD/BALANCE             [30,35)    [30,35)    reinterpreted
#     S9(7)V99 -> S9(6)V999: scale 2 -> 3, still 5 packed bytes, so the
#     field did NOT move; only its interpretation changed.
#   ACCT-RECORD/BALANCE-TEXT        --         [30,35)    added
#     new REDEFINES view over the BALANCE bytes; no extra storage.
#   ACCT-RECORD/CURRENCY            --         [35,38)    added
#   ACCT-RECORD/LAST-TXN-DATE       [35,43)    [38,46)    moved (+3)
#   ACCT-RECORD/LEGACY-CODE         [43,47)    [46,50)    moved (+3)
#   ACCT-RECORD/LEGACY-CODE-NUM     [43,47)    [46,50)    moved (+3)
#     the tail fields only shifted because CURRENCY was inserted in
#     front of them; their own PIC/scale are untouched.
#   ACCT-RECORD/OLD-STATUS          [47,49)    --         removed
#
# record length 50 -> 51: +3 (CURRENCY) - 2 (OLD-STATUS) = +1.
# BANKB keeps v1 for every date -> unaffected.

MAIN_FIELDS = {
    "ACCT-RECORD/ACCT-ID": "unchanged",
    "ACCT-RECORD/ACCT-NAME": "unchanged",
    "ACCT-RECORD/BALANCE": "reinterpreted",
    "ACCT-RECORD/BALANCE-TEXT": "added",
    "ACCT-RECORD/CURRENCY": "added",
    "ACCT-RECORD/LAST-TXN-DATE": "moved",
    "ACCT-RECORD/LEGACY-CODE": "moved",
    "ACCT-RECORD/LEGACY-CODE-NUM": "moved",
    "ACCT-RECORD/OLD-STATUS": "removed",
}

EXPECTED = {
    "main": {
        "exit_code": 1,
        "conclusion": "incompatible",
        "problems": [],
        "senders": {
            "BANKA": {
                "status": "incompatible",
                "affected": [("ACCT", "2024-09-01", None, "1", "2",
                              "incompatible")],
                "unaffected": [("ACCT", "2024-01-01", "2024-08-31", "1")],
            },
            "BANKB": {
                "status": "unaffected",
                "affected": [],
                "unaffected": [("ACCT", "2024-01-01", None, "1")],
            },
        },
        "comparison": {
            "key": ("BANKA", "ACCT"),
            "fields": MAIN_FIELDS,
            "record_length": (50, 51),
            "conclusion": "incompatible",
            "bytes": {  # spot-checked absolute byte ranges
                "ACCT-RECORD/BALANCE": ([30, 35], [30, 35]),
                "ACCT-RECORD/CURRENCY": (None, [35, 38]),
                "ACCT-RECORD/LAST-TXN-DATE": ([35, 43], [38, 46]),
                "ACCT-RECORD/OLD-STATUS": ([47, 49], None),
            },
            "view_clusters": [
                ("ACCT-RECORD/BALANCE", [30, 35], [],
                 ["ACCT-RECORD/BALANCE-TEXT"]),
            ],
        },
    },

    # CARD v2 adds a view and names the former FILLER bytes; record
    # length 13 -> 13; every pre-existing field untouched -> compatible.
    "compatible": {
        "exit_code": 0,
        "conclusion": "compatible",
        "problems": [],
        "senders": {
            "BANKC": {
                "status": "compatible",
                "affected": [("CARD", "2024-09-01", None, "1", "2",
                              "compatible")],
                "unaffected": [("CARD", "2024-01-01", "2024-08-31", "1")],
            },
        },
        "comparison": {
            "key": ("BANKC", "CARD"),
            "fields": {
                "CARD-RECORD/CARD-ID": "unchanged",
                "CARD-RECORD/AMOUNT": "unchanged",
                "CARD-RECORD/AMT-TEXT": "added",
                "CARD-RECORD/REGION": "added",
            },
            "record_length": (13, 13),
            "conclusion": "compatible",
            "bytes": {
                "CARD-RECORD/AMOUNT": ([6, 10], [6, 10]),
                "CARD-RECORD/REGION": (None, [10, 13]),
            },
            "view_clusters": [
                ("CARD-RECORD/AMOUNT", [6, 10], [],
                 ["CARD-RECORD/AMT-TEXT"]),
            ],
        },
    },

    # STATUS keeps name and bytes but moves group -> removed + added,
    # never "the same field"; record length 6 -> 6 but a removal is
    # breaking -> incompatible.
    "regroup": {
        "exit_code": 1,
        "conclusion": "incompatible",
        "problems": [],
        "senders": {
            "BANKD": {
                "status": "incompatible",
                "affected": [("GRID", "2024-09-01", None, "1", "2",
                              "incompatible")],
                "unaffected": [("GRID", "2024-01-01", "2024-08-31", "1")],
            },
        },
        "comparison": {
            "key": ("BANKD", "GRID"),
            "fields": {
                "GRID-RECORD/GRID-ID": "unchanged",
                "GRID-RECORD/GRP-OLD/STATUS": "removed",
                "GRID-RECORD/GRP-NEW/STATUS": "added",
            },
            "record_length": (6, 6),
            "conclusion": "incompatible",
            "bytes": {
                "GRID-RECORD/GRP-OLD/STATUS": ([4, 6], None),
                "GRID-RECORD/GRP-NEW/STATUS": (None, [4, 6]),
            },
            "view_clusters": [],
        },
    },

    # Overlap 2024-08-15..2024-08-31 (closed intervals) in the new
    # manifest: those dates must not be silently assigned a version.
    "conflict": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("overlap", "new_manifest", "BANKA", "ACCT",
             "2024-08-15", "2024-08-31"),
        ],
        "senders": {
            "BANKA": {
                "status": "unverifiable",
                "affected": [("ACCT", "2024-09-01", None, "1", "2",
                              "incompatible")],
                "unaffected": [("ACCT", "2024-01-01", "2024-08-14", "1")],
                "unresolvable": [("2024-08-15", "2024-08-31", "overlap")],
            },
        },
    },

    # September 2024 has no layout at all in the new manifest.
    "gap": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("gap", "new_manifest", "BANKA", "ACCT",
             "2024-09-01", "2024-09-30"),
        ],
        "senders": {
            "BANKA": {
                "status": "unverifiable",
                "affected": [("ACCT", "2024-10-01", None, "1", "2",
                              "incompatible")],
                "unaffected": [("ACCT", "2024-01-01", "2024-08-31", "1")],
                "unresolvable": [("2024-09-01", "2024-09-30",
                                  "gap_in_new")],
            },
        },
    },

    # BANKC/CARD exists only in the new manifest: no old baseline to
    # prove compatibility against.
    "nobaseline": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("no_old_baseline", "old_manifest", "BANKC", "CARD",
             "2024-01-01", None),
        ],
        "senders": {
            "BANKA": {
                "status": "unaffected",
                "affected": [],
                "unaffected": [("ACCT", "2024-01-01", None, "1")],
            },
            "BANKC": {
                "status": "unverifiable",
                "affected": [],
                "unaffected": [],
                "unresolvable": [("2024-01-01", None, "no_old_baseline")],
            },
        },
    },

    # The new manifest references copybooks/ACCT-9.cpy, which is absent.
    "missing_copybook": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("copybook_not_found", "new_manifest", "BANKA", "ACCT",
             "2024-09-01", None),
        ],
        "senders": {
            "BANKA": {
                "status": "unverifiable",
                "affected": [],
                "unaffected": [("ACCT", "2024-01-01", "2024-08-31", "1")],
                "unresolvable": [("2024-09-01", None,
                                  "copybook_unavailable")],
            },
        },
    },

    # GRP-A declared twice in the new v2 copybook: the group path and
    # the leaf path ACCT-RECORD/GRP-A/CODE are both ambiguous.
    "duplicate_path": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("duplicate_field_path", "new_manifest", "BANKA", "ACCT",
             None, None),
            ("duplicate_field_path", "new_manifest", "BANKA", "ACCT",
             None, None),
        ],
        "problem_paths": ["ACCT-RECORD/GRP-A", "ACCT-RECORD/GRP-A/CODE"],
        "senders": {
            "BANKA": {
                "status": "unverifiable",
                "affected": [("ACCT", "2024-09-01", None, "1", "2",
                              "unverifiable")],
                "unaffected": [("ACCT", "2024-01-01", "2024-08-31", "1")],
            },
        },
    },

    # Both manifests say "version 1" for every date, but the files
    # behind that version differ: a version must mean exactly one
    # layout.
    "version_reused": {
        "exit_code": 2,
        "conclusion": "unverifiable",
        "problems": [
            ("version_reused", None, "BANKA", "ACCT",
             "2024-01-01", None),
        ],
        "senders": {
            "BANKA": {
                "status": "unverifiable",
                "affected": [],
                "unaffected": [],
                "unresolvable": [("2024-01-01", None, "version_reused")],
            },
        },
    },

    # The output directory already exists: refuse to overwrite, exit 2,
    # no report files appear inside it.
    "existing_outdir": {
        "exit_code": 2,
        "no_report": True,
    },
}
