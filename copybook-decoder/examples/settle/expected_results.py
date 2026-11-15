"""Hand-written expectations for the settlement example scenarios.

Nothing in this module is produced by the settlement processor: every
value is derived by hand from the event narrative in build_events.py.
The runner compares the delivered result files against these literals.
"""

from __future__ import annotations

# Scenario name -> cutoff and the checks run_scenarios.py performs.
EXPECTED = {
    "hand": {
        "cutoff": "2026-01-10T00:00:00Z",
        "exit_code": 0,
        # (bill, receipt, amount, status, release_reason)
        "allocations": [
            ("A", "P", "100.00", "released", "receipt_cancelled"),
            ("B", "P", "50.00", "released", "receipt_cancelled"),
            ("B", "Q", "50.00", "released", "bill_cancelled"),
            ("A", "R", "60.00", "active", None),
            ("A", "Q", "40.00", "released", "receipt_cancelled"),
            ("C", "Q", "10.00", "released", "receipt_cancelled"),
        ],
        "active": [("A", "R", "60.00")],
        "balances": {                       # object_id -> (kind, remaining)
            "A": ("BILL", "40.00"),
            "B": ("BILL", "0.00"),
            "C": ("BILL", "100.00"),
            "P": ("RECEIPT", "0.00"),
            "Q": ("RECEIPT", "0.00"),
            "R": ("RECEIPT", "0.00"),
        },
        "conservation": {
            "bill_original": "200.00",     # A + C only (B cancelled)
            "bill_open": "140.00",
            "receipt_original": "60.00",   # R only (P, Q cancelled)
            "receipt_unallocated": "0.00",
            "active_allocation": "60.00",
        },
        # Checkpoints re-run with an earlier cutoff: second -> active edges.
        "checkpoints": {
            "2026-01-01T00:00:04Z": [
                ("A", "P", "100.00"), ("B", "P", "50.00"),
                ("B", "Q", "50.00")],
            "2026-01-01T00:00:07Z": [
                ("B", "Q", "50.00"), ("A", "R", "60.00")],
            "2026-01-01T00:00:08Z": [
                ("A", "R", "60.00"), ("A", "Q", "40.00"),
                ("C", "Q", "10.00")],
        },
    },
    "hand_shuffled": {
        "cutoff": "2026-01-10T00:00:00Z",
        "exit_code": 0,
        "must_match_scenario": "hand",     # byte-identical destinations
    },
    "early_receipt": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 0,
        "active": [("B", "P", "100.00")],
    },
    "same_instant": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 0,
        # UTF-8 byte order: B1 before B2, P1 before P2.
        "active": [("B1", "P1", "100.00"), ("B2", "P2", "100.00")],
    },
    "offsets": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 0,
        "active": [("B1", "P1", "10.00")],
        "at_utc": {
            "B1": "2026-01-01T00:00:00+00:00",
            "P1": "2026-01-01T00:00:00+00:00",
        },
    },
    "cutoff_equal": {
        "cutoff": "2026-03-01T12:00:00Z",
        "exit_code": 0,
        "active": [("B1", "P1", "10.00")],
        "applied_event_count": 2,
    },
    "aging": {
        "cutoff": "2026-03-31T12:00:00Z",
        "exit_code": 0,
        "base_day": "2026-03-31",
        "bucket_amounts": {
            "not_due": "200.00",
            "overdue_1_30": "100.00",
            "overdue_31_60": "100.00",
            "overdue_61_90": "100.00",
            "overdue_91_plus": "100.00",
        },
    },
    "unallocated": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 0,
        "active": [("B1", "P1", "100.00")],
        "receipt_unallocated": "25.50",
    },
    "huge": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 0,
        "amount_text": "9" * 30 + ".00",
        "amount_cents": int("9" * 30) * 100,
    },
    "bad_group": {
        "cutoff": "2026-02-01T00:00:00Z",
        "exit_code": 1,                     # business rejection
        "good_group": "GOOD",
        "good_active": [("GB", "GP", "10.00")],
        "bad_group": "BAD",
        "bad_error_codes": ["already_cancelled"],
        "bad_has_balances": False,
    },
    # Fatal runs: exit 2 and no output directory at all.
    "fatal_corrupt": {"cutoff": "2026-02-01T00:00:00Z", "exit_code": 2},
    "fatal_duplicate": {"cutoff": "2026-02-01T00:00:00Z", "exit_code": 2},
}
