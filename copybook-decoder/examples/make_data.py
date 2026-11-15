#!/usr/bin/env python3
"""Generate the example EBCDIC data files under examples/data/.

Two files are produced in the *same* directory to mimic a real landing
zone where layout generations mix:

* BANKA-20240315.dat  -- layout version 1 (business date before cutover)
* BANKA-20241015.dat  -- layout version 2 (after cutover)

Each file contains good records, one record with a corrupt packed-decimal
field, and a truncated final record, to exercise quarantine.
"""

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def ebcdic(text: str) -> bytes:
    return text.encode("cp037")


def zoned(value: int, length: int) -> bytes:
    return f"{value:0{length}d}".encode("cp037")


def comp3(value: int, num_digits: int) -> bytes:
    sign_nibble = 0xD if value < 0 else 0xC
    digits = f"{abs(value):0{num_digits}d}"
    if len(digits) % 2 == 0:
        digits = "0" + digits
    out = bytearray()
    for i in range(0, len(digits) - 1, 2):
        out.append((int(digits[i]) << 4) | int(digits[i + 1]))
    out.append((int(digits[-1]) << 4) | sign_nibble)
    return bytes(out)


def rec_v1(acct_id, name, balance, txn_date, legacy):
    return (ebcdic(acct_id.ljust(10)) + ebcdic(name.ljust(20))
            + comp3(balance, 9) + zoned(txn_date, 8)
            + ebcdic(legacy.ljust(4)) + ebcdic("   "))


def rec_v2(acct_id, name, balance, currency, txn_date, legacy):
    return (ebcdic(acct_id.ljust(10)) + ebcdic(name.ljust(20))
            + comp3(balance, 11) + ebcdic(currency.ljust(3))
            + zoned(txn_date, 8) + ebcdic(legacy.ljust(4)) + ebcdic("  "))


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    # --- version 1 file: 3 good, 1 corrupt BALANCE, 1 truncated ---------
    bad_balance = (ebcdic("0000000004") + ebcdic("CORRUPT RECORD".ljust(20))
                   + b"\xff\xff\xff\xff\xff" + zoned(20240315, 8)
                   + ebcdic("9   ") + ebcdic("   "))
    truncated = rec_v1("0000000005", "SHORT RECORD", 100, 20240315, "7")[:30]
    lines_v1 = [
        rec_v1("0000000001", "ALICE ANDERSON", 123456, 20240314, "12AB"),
        rec_v1("0000000002", "BOB BAKER", -5000, 20240314, "0034"),
        bad_balance,
        rec_v1("0000000003", "CAROL CLARK", 9876543, 20240315, "ZZZZ"),
        truncated,
    ]
    (DATA_DIR / "BANKA-20240315.dat").write_bytes(b"\n".join(lines_v1))

    # --- version 2 file: 2 good, 1 corrupt, 1 truncated -----------------
    bad_balance_v2 = (ebcdic("0000000003") + ebcdic("CORRUPT ROW".ljust(20))
                      + b"\x00\x01\x02\x03\x04\x05" + ebcdic("EUR")
                      + zoned(20241015, 8) + ebcdic("77  ") + ebcdic("  "))
    truncated_v2 = rec_v2("0000000004", "SHORT ROW", 100, "USD",
                          20241015, "1")[:25]
    lines_v2 = [
        rec_v2("0000000001", "ALICE ANDERSON", 200000, "USD", 20241014,
               "12AB"),
        rec_v2("0000000002", "BOB BAKER", -123456789, "EUR", 20241014,
               "0034"),
        bad_balance_v2,
        truncated_v2,
    ]
    (DATA_DIR / "BANKA-20241015.dat").write_bytes(b"\n".join(lines_v2))

    for path in sorted(DATA_DIR.glob("*.dat")):
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
