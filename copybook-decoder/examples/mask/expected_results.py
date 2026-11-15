#!/usr/bin/env python3
"""Hand-written expectations for every mask scenario.

How the expectations were derived
---------------------------------
Counts, rejection reasons, bucket labels, null/missing behaviour, join
relations and file inventories below are literal values worked out by
hand from the scenario data and the policy.

The masked *values* depend on HMAC-SHA256 and cannot literally be
computed with pen and paper.  They are instead produced by the
**independent reference implementation** in this file, written directly
from the documented algorithm specification (domain separator, message
layout, counter-mode keystream, rejection sampling, exact rational
amount rounding, synthetic name pools).  It shares no code with
``copybook.mask``; ``run_scenarios.py`` asserts that the real engine
output matches this reference field by field.  If the implementation
and the spec ever diverge, the comparison fails.

Nothing here reads or imports the package under test.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

EXIT_OK = 0
EXIT_FAILED = 2

# The demonstration key (examples/mask/demo_key.txt).  Demo data only --
# a real deployment supplies the key from its own environment or vault.
KEY_TEXT = "DEMO-ONLY-mask-key-2024-not-for-production"
KEY = KEY_TEXT.encode("utf-8")
KEY_FINGERPRINT = "sha256:aba28c86450ca4e6"

DOMAIN = "copybook-mask/v1"

# Synthetic name pools, copied from the algorithm specification.
GIVEN_NAMES = (
    "MIRA", "ALDEN", "TAVIA", "ORIN", "SELKIE", "BRANN", "CATO", "DELIA",
    "EMRIC", "FABLE", "SORREL", "PIKE", "NOVA", "HALDEN", "ISOLDE", "JORY",
    "KESTREL", "LIOR", "MAVEN", "NIALA", "OSTEN", "PEREN", "QUILLA",
    "ROAN", "SABLE", "TAMSIN", "ULRIC", "VESNA", "WREN", "YARDEN",
    "ZEPHIR", "ONDRA",
)
FAMILY_NAMES = (
    "HOLT", "FERRAN", "QUILL", "MARLOW", "ROOK", "HALVARD", "SORREL",
    "PIKE", "NORRIS", "TALBOT", "VERROW", "ASHDOWN", "BRISCO", "CARN",
    "DELVEC", "ELSTOW", "FENWICK", "GARROW", "HARN", "INKWELL", "JESSOP",
    "KELD", "LARKSPUR", "MERE", "NACKER", "OSGOOD", "PERNELL", "QUARLES",
    "RUSHFEN", "SKELTON", "THORNCROFT", "UMBER",
)

_ALPHABETS = {
    "digit": "0123456789",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "lower": "abcdefghijklmnopqrstuvwxyz",
}


# ------------------------------------------------ reference implementation

def _keystream(msg: bytes):
    counter = 0
    while True:
        block = hmac.new(KEY, msg + b"#" + str(counter).encode("ascii"),
                         hashlib.sha256).digest()
        yield from block
        counter += 1


def _draw_char(stream, alphabet: str) -> str:
    n = len(alphabet)
    limit = (256 // n) * n
    for byte in stream:
        if byte < limit:
            return alphabet[byte % n]
    raise AssertionError("unreachable")


def ref_pseudonym(group: str, text: str) -> str:
    msg = f"{DOMAIN}|pseudonym|{group}|{text}".encode("utf-8")
    stream = _keystream(msg)
    out = []
    for ch in text:
        if "0" <= ch <= "9":
            out.append(_draw_char(stream, _ALPHABETS["digit"]))
        elif "A" <= ch <= "Z":
            out.append(_draw_char(stream, _ALPHABETS["upper"]))
        elif "a" <= ch <= "z":
            out.append(_draw_char(stream, _ALPHABETS["lower"]))
        else:
            out.append(ch)
    return "".join(out)


def ref_synthesize(scope: str, original: str) -> str:
    import re
    for attempt in range(1000):
        msg = (f"{DOMAIN}|synthesize|{scope}|{original}|{attempt}"
               .encode("utf-8"))
        digest = hmac.new(KEY, msg, hashlib.sha256).digest()
        given = GIVEN_NAMES[digest[0] % len(GIVEN_NAMES)]
        family = FAMILY_NAMES[digest[1] % len(FAMILY_NAMES)]
        seq = int.from_bytes(digest[2:6], "big") % 10000
        candidate = f"{given} {family} {seq:04d}"
        haystack = candidate.casefold()
        whole = original.strip().casefold()
        leaked = len(whole) >= 2 and whole in haystack
        if not leaked:
            for token in re.split(r"[^0-9A-Za-z]+", original):
                if len(token) >= 2 and token.casefold() in haystack:
                    leaked = True
                    break
        if not leaked:
            return candidate
    raise AssertionError("reference could not synthesize")


def ref_shift_amount(scope: str, value_text: str, max_pct: str) -> str:
    value = Decimal(value_text)
    scale = max(0, -value.as_tuple().exponent)
    msg = f"{DOMAIN}|shift-amount|{scope}|{value}".encode("utf-8")
    digest = hmac.new(KEY, msg, hashlib.sha256).digest()
    num = int.from_bytes(digest[:8], "big")
    frac = Fraction(2 * num - (1 << 64), 1 << 64)
    delta = Fraction(value) * Fraction(Decimal(max_pct)) / 100 * frac
    scaled = (Fraction(value) + delta) * (10 ** scale)
    if scaled >= 0:
        rounded = math.floor(scaled + Fraction(1, 2))
    else:
        rounded = -math.floor(-scaled + Fraction(1, 2))
    digits = tuple(int(d) for d in str(abs(rounded)))
    return str(Decimal((1 if rounded < 0 else 0, digits, -scale)))


def ref_shift_date(scope: str, original_text: str, max_days: int) -> str:
    msg = f"{DOMAIN}|shift-date|{scope}|{original_text}".encode("utf-8")
    n = 2 * max_days + 1
    limit = ((1 << 64) // n) * n
    counter = 0
    while True:
        digest = hmac.new(KEY, msg + b"#" + str(counter).encode("ascii"),
                          hashlib.sha256).digest()
        v = int.from_bytes(digest[:8], "big")
        if v < limit:
            offset = v % n - max_days
            break
        counter += 1
    day = date(*[int(p) for p in original_text.split("-")])
    return (day + timedelta(days=offset)).isoformat()


# ------------------------------------------------------- derived constants

# Stable pseudonyms for every distinct account number in the data.
ACCT_FAKES = {
    original: ref_pseudonym("acct", original)
    for original in ("1000002345", "1000002346", "1000002347",
                     "1000002348", "1000002999")
}

NAME_FAKES = {
    original: ref_synthesize("person", original)
    for original in ("ZHANG WEI", "LI NA", "WANG FANG")
}

DATE_FAKES = {
    original: ref_shift_date("bizdate", original, 30)
    for original in ("2024-01-15", "2024-01-16", "2023-12-31",
                     "2024-02-01", "2024-03-05")
}

AMOUNT_FAKES = {
    original: ref_shift_amount("TXN-AMOUNT", original, "5")
    for original in ("120.50", "-30.00", "1000000.00", "0.01")
}


# ---------------------------------------------------- expected main output

def _acct(line, acct, name, balance, open_date, branch, alt="present"):
    fields = {
        "ACCT-NO": ACCT_FAKES[acct],
        "BALANCE": balance,
        "OPEN-DATE": DATE_FAKES[open_date],
        "BRANCH-CODE": branch,
    }
    if name != "missing":
        fields["CUST-NAME"] = NAME_FAKES[name] if name is not None else None
    if alt == "present":
        fields["ALT-ACCT"] = ACCT_FAKES[acct]
    return {"line": line, "fields": fields}


def _txn(line, acct, amount, txn_date):
    return {"line": line, "fields": {
        "ACCT-NO": ACCT_FAKES[acct],
        "TXN-AMOUNT": AMOUNT_FAKES[amount],
        "TXN-DATE": DATE_FAKES[txn_date],
    }}


def _holder(line, acct, name, since_label):
    return {"line": line, "fields": {
        "ACCT-NO": ACCT_FAKES[acct],
        "HOLDER-NAME": NAME_FAKES[name],
        "HOLDER-SINCE": since_label,
    }}


# Bucket labels worked out by hand from the declared boundaries
# ["1000.00", "10000.00", "100000.00"]:
#   12345.67  -> [10000, 100000)   "10000.00-100000.00"
#     999.99  -> < 1000            "<1000.00"
#  250000.00  -> >= 100000         ">=100000.00"
#    5000.00  -> [1000, 10000)     "1000.00-10000.00"
# Month labels: 2024-01-15 -> "2024-01", 2022-06-30 -> "2022-06".
EXPECTED_MAIN = {
    "exit_code": EXIT_OK,
    "summary": {
        "file_count": 3,
        "records_in": 11,
        "records_out": 10,
        "records_rejected": 1,
        "rule_count": 9,
        "group_count": 1,
        "distinct_pseudonym_values": 5,
    },
    "output_files": ["accounts.masked.jsonl", "mask_report.json",
                     "mask_report.txt", "record_report.jsonl",
                     "related.masked.jsonl", "txns.masked.jsonl"],
    "accounts": [
        _acct(1, "1000002345", "ZHANG WEI", "10000.00-100000.00",
              "2024-01-15", "BR01"),
        _acct(2, "1000002346", "LI NA", "<1000.00",
              "2024-01-15", "BR02"),
        _acct(3, "1000002345", None, ">=100000.00",
              "2023-12-31", "BR01"),
        _acct(4, "1000002347", "missing", "1000.00-10000.00",
              "2024-02-01", "BR03", alt="missing"),
        # line 5 rejected: ALT-ACCT is null and the policy says on_null=reject
    ],
    "txns": [
        _txn(1, "1000002345", "120.50", "2024-01-15"),
        _txn(2, "1000002345", "-30.00", "2024-01-16"),
        _txn(3, "1000002346", "1000000.00", "2024-01-15"),
        _txn(4, "1000002999", "0.01", "2024-02-01"),
    ],
    "related": [
        _holder(1, "1000002345", "ZHANG WEI", "2024-01"),
        _holder(2, "1000002346", "LI NA", "2022-06"),
    ],
    "rejections": [
        {"line_number": 5, "path": ["ALT-ACCT"],
         "reason": "null value at path 'ALT-ACCT' refused by policy "
                   "(on_null=reject)"},
    ],
    "file_counts": {
        "accounts": {"records_in": 5, "records_out": 4,
                     "records_rejected": 1},
        "related": {"records_in": 2, "records_out": 2,
                    "records_rejected": 0},
        "txns": {"records_in": 4, "records_out": 4,
                 "records_rejected": 0},
    },
    # Per-rule scan statistics, counted by hand over all 11 input records
    # (5 accounts + 4 txns + 2 related).  "missing" means the path does
    # not resolve in that record at all -- e.g. ALT-ACCT is absent from
    # accounts line 4 and from every txn/holder record: 1 + 4 + 2 = 7.
    "rule_stats": {
        "ACCT-NO": {"matched_records": 11, "matched_values": 11,
                    "nulls": 0, "empties": 0, "missing_records": 0},
        "ALT-ACCT": {"matched_records": 4, "matched_values": 3,
                     "nulls": 1, "empties": 0, "missing_records": 7},
        "CUST-NAME": {"matched_records": 4, "matched_values": 3,
                      "nulls": 1, "empties": 0, "missing_records": 7},
        "HOLDER-NAME": {"matched_records": 2, "matched_values": 2,
                        "nulls": 0, "empties": 0, "missing_records": 9},
        "BALANCE": {"matched_records": 5, "matched_values": 5,
                    "nulls": 0, "empties": 0, "missing_records": 6},
        "TXN-AMOUNT": {"matched_records": 4, "matched_values": 4,
                       "nulls": 0, "empties": 0, "missing_records": 7},
        "OPEN-DATE": {"matched_records": 5, "matched_values": 5,
                      "nulls": 0, "empties": 0, "missing_records": 6},
        "TXN-DATE": {"matched_records": 4, "matched_values": 4,
                     "nulls": 0, "empties": 0, "missing_records": 7},
        "HOLDER-SINCE": {"matched_records": 2, "matched_values": 2,
                         "nulls": 0, "empties": 0, "missing_records": 9},
    },
    # Structural facts checked literally, independent of the reference:
    # distinct originals never merge, and the fake keeps the format.
    "distinct_fakes": True,
    "fake_shape": {"length": 10, "all_digits": True},
    # Original sensitive strings that must appear nowhere in the output.
    "forbidden_fragments": ["1000002345", "1000002346", "1000002347",
                            "1000002348", "1000002999", "ZHANG", "WEI",
                            "LI NA", "WANG FANG", "12345.67", "250000.00"],
}

# Failure scenarios: every one must exit 2 and leave no output directory.
EXPECTED_FAILURES = {
    "policy_path_never_matched": {
        "exit_code": EXIT_FAILED,
        "stderr_contains": "never matched any record",
    },
    "policy_type_mismatch": {
        "exit_code": EXIT_FAILED,
        "stderr_contains": "plain decimal string",
    },
    "wrong_key": {
        "exit_code": EXIT_FAILED,
        "stderr_contains": "does not match the key_fingerprint",
    },
    "corrupt_input": {
        "exit_code": EXIT_FAILED,
        "stderr_contains": "invalid JSON",
    },
    "existing_output": {
        "exit_code": EXIT_FAILED,
        "stderr_contains": "already exists",
    },
}
