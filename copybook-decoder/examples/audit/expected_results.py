#!/usr/bin/env python3
"""Hand-written expectations for every audit scenario.

These numbers were derived from the handoff protocol description on paper,
line by line, before the validator was ever run against the files.  The
runner in ``run_scenarios.py`` asserts that the real audit output matches
this table; nothing here is computed from audit results.

Conventions used below
----------------------
* physical line numbers start at 1 and include quarantined lines;
* ``actual_detail_count`` counts DETAIL records *plus* in-batch quarantine
  positions within a batch;
* quantity totals are expressed in exact milli-units (1.000 -> 1000);
* exit code 0 = all pass, 1 = audited but business rejection,
  2 = input/config cannot be validated at all (no delivery written).
"""

# Exit codes mirrored from copybook.__main__
EXIT_PASS = 0
EXIT_BUSINESS_REJECT = 1
EXIT_CANNOT_VALIDATE = 2

EXPECTED = {

    # ---- handoff story, part 1 -----------------------------------------
    # Physical layout (9 lines):
    #   1 FHEAD F-2024-001
    #   2 BHEAD BAT-01
    #   3 <truncated, quarantined>   <- one received detail position, values
    #                                   unknown; BAT-01 count still sees it
    #   4 BTAIL BAT-01 dc=1 aaa=0 bbb=0
    #   5 BHEAD BAT-02
    #   6 DETAIL seq=1 AAA  2.500
    #   7 DETAIL seq=2 BBB -1.250
    #   8 BTAIL BAT-02 dc=2 aaa=2.500 bbb=-1.250
    #   9 FTAIL bc=2 dc=3             (3 = quarantine + 2 real details)
    #
    # BAT-01 is refused on the quarantine line: row count 1 vs 1 matches,
    # but category totals are undecidable (the hidden line is not zero).
    # BAT-02 is internally perfect, so its two details remain deliverable.
    # FTAIL counts are honest, so the file itself is not rejected.
    "handoff_good": {
        "physical_lines": 9,
        "quarantine_lines": [3],
        "exit_code": EXIT_BUSINESS_REJECT,
        "conclusion": "business_rejected",
        "file_rejected": False,
        "accepted_lines": [6, 7],
        "batches": [
            {"ordinal": 1, "batch_id": "BAT-01", "status": "rejected",
             "bhead_line": 2, "btail_line": 4,
             "declared_detail_count": "1", "actual_detail_count": 1,
             "detail_count_ok": True, "quantity_status": "undecidable",
             "diagnostic_codes": ["quarantined_detail"]},
            {"ordinal": 2, "batch_id": "BAT-02", "status": "accepted",
             "bhead_line": 5, "btail_line": 8,
             "declared_detail_count": "2", "actual_detail_count": 2,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 2500, "BBB": -1250}},
        ],
        "ftail": {"line": 9, "batch_count_declared": "2",
                  "batch_count_actual": 2,
                  "detail_count_declared": "3",
                  "detail_count_actual": 3},
    },

    # ---- handoff story, part 2 -----------------------------------------
    # Same nine physical lines, only line 9 changes: FTAIL now declares
    # detail_count=2, pretending the quarantined line never existed. The
    # engine still counts three in-batch positions, so this is a file-level
    # fault discovered at the last line. The earlier "good" BAT-02 keeps its
    # own batch check result, but its acceptance is revoked: the delivered
    # detail file must be empty.
    "handoff_ftail_lied": {
        "physical_lines": 9,
        "quarantine_lines": [3],
        "exit_code": EXIT_BUSINESS_REJECT,
        "conclusion": "business_rejected",
        "file_rejected": True,
        "accepted_lines": [],
        "batches": [
            {"ordinal": 1, "batch_id": "BAT-01", "status": "rejected",
             "bhead_line": 2, "btail_line": 4,
             "declared_detail_count": "1", "actual_detail_count": 1,
             "detail_count_ok": True, "quantity_status": "undecidable",
             "diagnostic_codes": ["quarantined_detail"]},
            {"ordinal": 2, "batch_id": "BAT-02", "status": "accepted",
             "bhead_line": 5, "btail_line": 8,
             "declared_detail_count": "2", "actual_detail_count": 2,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 2500, "BBB": -1250}},
        ],
        "ftail": {"line": 9, "batch_count_declared": "2",
                  "batch_count_actual": 2,
                  "detail_count_declared": "2",
                  "detail_count_actual": 3},
        "file_diagnostic_codes": ["ftail_detail_count_mismatch"],
    },

    # ---- empty batch ----------------------------------------------------
    # 1 FHEAD, 2 BHEAD EB-1, 3 BTAIL dc=0 (all totals zero), 4 FTAIL bc=1
    # dc=0. No details is a legal batch; zero counts reconcile exactly.
    "empty_batch": {
        "physical_lines": 4,
        "quarantine_lines": [],
        "exit_code": EXIT_PASS,
        "conclusion": "pass",
        "file_rejected": False,
        "accepted_lines": [],
        "batches": [
            {"ordinal": 1, "batch_id": "EB-1", "status": "accepted",
             "bhead_line": 2, "btail_line": 3,
             "declared_detail_count": "0", "actual_detail_count": 0,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 0, "BBB": 0}},
        ],
        "ftail": {"line": 4, "batch_count_declared": "1",
                  "batch_count_actual": 1,
                  "detail_count_declared": "0",
                  "detail_count_actual": 0},
    },

    # ---- negative quantities cancel exactly -----------------------------
    # Lines 3 and 4 are +10.001 and -10.001 in category AAA. In milli-units
    # 10001 + (-10001) = 0 exactly; BTAIL declares AAA total 0. No float is
    # involved, so the cancellation must reconcile rather than leave a
    # residual fraction.
    "negative_offset": {
        "physical_lines": 6,
        "quarantine_lines": [],
        "exit_code": EXIT_PASS,
        "conclusion": "pass",
        "file_rejected": False,
        "accepted_lines": [3, 4],
        "batches": [
            {"ordinal": 1, "batch_id": "NB-1", "status": "accepted",
             "bhead_line": 2, "btail_line": 5,
             "declared_detail_count": "2", "actual_detail_count": 2,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 0, "BBB": 0}},
        ],
        "ftail": {"line": 6, "batch_count_declared": "1",
                  "batch_count_actual": 1,
                  "detail_count_declared": "2",
                  "detail_count_actual": 2},
    },

    # ---- three decimal places -------------------------------------------
    # 0.001 + 0.002 = 0.003 in BBB. Binary float would drift; the milli-unit
    # integer sum is exactly 3 and matches the BTAIL declaration 0.003.
    "three_decimals": {
        "physical_lines": 6,
        "quarantine_lines": [],
        "exit_code": EXIT_PASS,
        "conclusion": "pass",
        "file_rejected": False,
        "accepted_lines": [3, 4],
        "batches": [
            {"ordinal": 1, "batch_id": "MB-1", "status": "accepted",
             "bhead_line": 2, "btail_line": 5,
             "declared_detail_count": "2", "actual_detail_count": 2,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 0, "BBB": 3}},
        ],
        "ftail": {"line": 6, "batch_count_declared": "1",
                  "batch_count_actual": 1,
                  "detail_count_declared": "2",
                  "detail_count_actual": 2},
    },

    # ---- seq restarts at 1 across batches -------------------------------
    # S1 contains seq 1,2; S2 starts again at seq 1. Per-batch numbering is
    # required, so both batches accept; deliverable lines are 3,4 and 7.
    "seq_restart": {
        "physical_lines": 9,
        "quarantine_lines": [],
        "exit_code": EXIT_PASS,
        "conclusion": "pass",
        "file_rejected": False,
        "accepted_lines": [3, 4, 7],
        "batches": [
            {"ordinal": 1, "batch_id": "S1", "status": "accepted",
             "bhead_line": 2, "btail_line": 5,
             "declared_detail_count": "2", "actual_detail_count": 2,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 3000, "BBB": 0}},
            {"ordinal": 2, "batch_id": "S2", "status": "accepted",
             "bhead_line": 6, "btail_line": 8,
             "declared_detail_count": "1", "actual_detail_count": 1,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 5000, "BBB": 0}},
        ],
        "ftail": {"line": 9, "batch_count_declared": "2",
                  "batch_count_actual": 2,
                  "detail_count_declared": "3",
                  "detail_count_actual": 3},
    },

    # ---- duplicate batch id ---------------------------------------------
    # BHEAD "DUP" appears at lines 2 and 5. Batch ids must be unique within
    # the file: this cannot be contained to one batch, so the whole file is
    # refused and no detail (lines 3, 6) is deliverable.
    "duplicate_batch": {
        "physical_lines": 8,
        "quarantine_lines": [],
        "exit_code": EXIT_BUSINESS_REJECT,
        "conclusion": "business_rejected",
        "file_rejected": True,
        "accepted_lines": [],
        "batches": [
            {"ordinal": 1, "batch_id": "DUP", "status": "accepted",
             "bhead_line": 2, "btail_line": 4,
             "declared_detail_count": "1", "actual_detail_count": 1,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 1000, "BBB": 0}},
            {"ordinal": 2, "batch_id": "DUP", "status": "accepted",
             "bhead_line": 5, "btail_line": 7,
             "declared_detail_count": "1", "actual_detail_count": 1,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 0, "BBB": 2000}},
        ],
        "ftail": {"line": 8, "batch_count_declared": "2",
                  "batch_count_actual": 2,
                  "detail_count_declared": "2",
                  "detail_count_actual": 2},
        "file_diagnostic_codes": ["duplicate_batch_id"],
    },

    # ---- orphan quarantine line outside any batch -----------------------
    # Line 2 is quarantined before any BHEAD exists. Its values can never be
    # reconciled to a BTAIL, so the file is refused outright; the FTAIL zero
    # counts do not rescue it.
    "orphan_quarantine": {
        "physical_lines": 3,
        "quarantine_lines": [2],
        "exit_code": EXIT_BUSINESS_REJECT,
        "conclusion": "business_rejected",
        "file_rejected": True,
        "accepted_lines": [],
        "batches": [],
        "ftail": {"line": 3, "batch_count_declared": "0",
                  "batch_count_actual": 0,
                  "detail_count_declared": "0",
                  "detail_count_actual": 0},
        "file_diagnostic_codes": ["orphan_quarantine"],
    },

    # ---- record appended after FTAIL ------------------------------------
    # Line 5 is a DETAIL appearing after the unique FTAIL on line 4. The
    # protocol ends at FTAIL; the extra line rejects the whole file and the
    # empty batch cannot be delivered either.
    "data_after_ftail": {
        "physical_lines": 5,
        "quarantine_lines": [],
        "exit_code": EXIT_BUSINESS_REJECT,
        "conclusion": "business_rejected",
        "file_rejected": True,
        "accepted_lines": [],
        "batches": [
            {"ordinal": 1, "batch_id": "AB-1", "status": "accepted",
             "bhead_line": 2, "btail_line": 3,
             "declared_detail_count": "0", "actual_detail_count": 0,
             "detail_count_ok": True, "quantity_status": "ok",
             "diagnostic_codes": [],
             "totals_milli": {"AAA": 0, "BBB": 0}},
        ],
        "ftail": {"line": 4, "batch_count_declared": "1",
                  "batch_count_actual": 1,
                  "detail_count_declared": "0",
                  "detail_count_actual": 0},
        "file_diagnostic_codes": ["data_after_ftail"],
    },

    # ---- evidence line gap ----------------------------------------------
    # The source .dat is the same nine-line handoff_good file, but the
    # runner deletes one decoded evidence row while still claiming
    # expected_lines=9. Physical coverage 1..9 is broken: the run is
    # UNVERIFIABLE (exit 2), and no delivery directory may appear.
    "line_gap": {
        "physical_lines": 9,
        "quarantine_lines": [3],
        "exit_code": EXIT_CANNOT_VALIDATE,
        "conclusion": None,
        "file_rejected": None,
        "accepted_lines": None,
        "delivers_output": False,
        "input_error": True,
    },
}
