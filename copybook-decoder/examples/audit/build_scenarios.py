#!/usr/bin/env python3
"""Build EBCDIC handoff files for every documented audit scenario.

The builder only knows how to emit the 72-byte HANDOFF-1 physical records
(and deliberately malformed lines for quarantine); all expectations about
batch status, control counts and deliverable line numbers live separately
in ``expected_results.py`` and were written by hand from the protocol
rules -- they are never derived from the validator under test.

Run:  python3 examples/audit/build_scenarios.py [out_dir]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

RECORD_LENGTH = 73
CODEC = "cp037"


def _text(value: str, width: int) -> bytes:
    value = "" if value is None else str(value)
    if len(value) > width:
        raise ValueError(f"value {value!r} wider than {width}")
    return value.ljust(width).encode(CODEC)


def _zoned(value: int, width: int) -> bytes:
    text = f"{abs(value):0{width}d}"
    if len(text) != width:
        raise ValueError(f"numeric {value} does not fit PIC 9({width})")
    return text.encode(CODEC)


def _comp3_milli(value: int, num_digits: int = 12) -> bytes:
    """Encode an integer milli-unit value as S9(9)V999 COMP-3 (7 bytes)."""
    sign_nibble = 0xD if value < 0 else 0xC
    digits = f"{abs(value):0{num_digits}d}"
    if len(digits) % 2 == 0:
        digits = "0" + digits
    out = bytearray()
    for i in range(0, len(digits) - 1, 2):
        out.append((int(digits[i]) << 4) | int(digits[i + 1]))
    out.append((int(digits[-1]) << 4) | sign_nibble)
    return bytes(out)


@dataclass
class HandoffBuilder:
    lines: list[bytes] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    # --------------------------------------------------------- records

    def _emit(self, parts: list[bytes], label: str) -> int:
        record = b"".join(parts)
        assert len(record) == RECORD_LENGTH, (label, len(record))
        self.lines.append(record)
        self.labels.append(label)
        return len(self.lines)

    def fhead(self, file_id: str) -> int:
        return self._emit([
            _text("FHEAD", 6), _text(file_id, 12), _text("", 10),
            _zoned(0, 6), _text("", 3), _comp3_milli(0), _zoned(0, 7),
            _comp3_milli(0), _comp3_milli(0), _zoned(0, 5), _text("", 3),
        ], f"FHEAD {file_id}")

    def ftail(self, file_id: str, batch_count: int, detail_count: int) -> int:
        return self._emit([
            _text("FTAIL", 6), _text(file_id, 12), _text("", 10),
            _zoned(0, 6), _text("", 3), _comp3_milli(0),
            _zoned(detail_count, 7),
            _comp3_milli(0), _comp3_milli(0), _zoned(batch_count, 5),
            _text("", 3),
        ], f"FTAIL bc={batch_count} dc={detail_count}")

    def bhead(self, batch_id: str) -> int:
        return self._emit([
            _text("BHEAD", 6), _text("", 12), _text(batch_id, 10),
            _zoned(0, 6), _text("", 3), _comp3_milli(0), _zoned(0, 7),
            _comp3_milli(0), _comp3_milli(0), _zoned(0, 5), _text("", 3),
        ], f"BHEAD {batch_id}")

    def detail(self, seq: int, batch_id: str, category: str,
               quantity_milli: int) -> int:
        return self._emit([
            _text("DETAIL", 6), _text("", 12), _text(batch_id, 10),
            _zoned(seq, 6), _text(category, 3),
            _comp3_milli(quantity_milli), _zoned(0, 7),
            _comp3_milli(0), _comp3_milli(0), _zoned(0, 5), _text("", 3),
        ], f"DETAIL seq={seq} {category} q={quantity_milli}")

    def btail(self, batch_id: str, detail_count: int,
              total_aaa_milli: int, total_bbb_milli: int) -> int:
        return self._emit([
            _text("BTAIL", 6), _text("", 12), _text(batch_id, 10),
            _zoned(0, 6), _text("", 3), _comp3_milli(0),
            _zoned(detail_count, 7),
            _comp3_milli(total_aaa_milli), _comp3_milli(total_bbb_milli),
            _zoned(0, 5), _text("", 3),
        ], (f"BTAIL {batch_id} dc={detail_count} "
            f"aaa={total_aaa_milli} bbb={total_bbb_milli}"))

    # ------------------------------------------------- malformed lines

    def truncated_line(self, label: str) -> int:
        """A 41-byte fragment: decoder quarantines it as 'truncated'."""
        fragment = (_text("DETAIL", 6) + _text("", 12) + _text("BX", 10)
                    + _zoned(1, 6) + _text("AAA", 3) + b"\x00" * 4)
        assert len(fragment) == 41
        self.lines.append(fragment)
        self.labels.append(label)
        return len(self.lines)

    def corrupt_comp3_line(self, seq: int, batch_id: str) -> int:
        """Full-width line whose QUANTITY has invalid packed nibbles."""
        record = (
            _text("DETAIL", 6) + _text("", 12) + _text(batch_id, 10)
            + _zoned(seq, 6) + _text("AAA", 3)
            + b"\xff\xff\xff\xff\xff\xff\xff"      # illegal COMP-3 bytes
            + _zoned(0, 7) + _comp3_milli(0) + _comp3_milli(0)
            + _zoned(0, 5) + _text("", 3))
        assert len(record) == RECORD_LENGTH
        self.lines.append(record)
        self.labels.append(f"DETAIL corrupt COMP-3 seq={seq}")
        return len(self.lines)

    def unknown_type_line(self) -> int:
        return self._emit([
            _text("WAT??", 6), _text("", 12), _text("", 10),
            _zoned(0, 6), _text("", 3), _comp3_milli(0), _zoned(0, 7),
            _comp3_milli(0), _comp3_milli(0), _zoned(0, 5), _text("", 3),
        ], "unknown record type WAT??")

    def write(self, path: Path) -> None:
        path.write_bytes(b"\n".join(self.lines) + b"\n")


# ------------------------------------------------------------ scenarios

def scenario_handoff_good() -> HandoffBuilder:
    """First batch: one quarantined detail. Second batch: fully correct."""
    b = HandoffBuilder()
    b.fhead("F-2024-001")
    b.bhead("BAT-01")
    b.truncated_line("quarantined detail (truncated) in BAT-01")
    b.btail("BAT-01", detail_count=1, total_aaa_milli=0,
            total_bbb_milli=0)
    b.bhead("BAT-02")
    b.detail(1, "BAT-02", "AAA", 2500)        # 2.500
    b.detail(2, "BAT-02", "BBB", -1250)       # -1.250
    b.btail("BAT-02", detail_count=2, total_aaa_milli=2500,
            total_bbb_milli=-1250)
    b.ftail("F-2024-001", batch_count=2, detail_count=3)
    return b


def scenario_handoff_ftail_lied() -> HandoffBuilder:
    """Same physical content, but FTAIL detail_count omits the bad line."""
    b = scenario_handoff_good()
    # rewrite only the last physical line's declared detail_count 3 -> 2
    b.lines[-1] = (
        _text("FTAIL", 6) + _text("F-2024-001", 12) + _text("", 10)
        + _zoned(0, 6) + _text("", 3) + _comp3_milli(0)
        + _zoned(2, 7)                          # lie: only 2 declared
        + _comp3_milli(0) + _comp3_milli(0) + _zoned(2, 5)
        + _text("", 3))
    b.labels[-1] = "FTAIL with wrong detail_count=2 (real: 3)"
    return b


def scenario_empty_batch() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-EMPTY")
    b.bhead("EB-1")
    b.btail("EB-1", 0, 0, 0)
    b.ftail("F-EMPTY", batch_count=1, detail_count=0)
    return b


def scenario_negative_offset() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-NEG")
    b.bhead("NB-1")
    b.detail(1, "NB-1", "AAA", 10001)     # 10.001
    b.detail(2, "NB-1", "AAA", -10001)    # -10.001 -> exact zero sum
    b.btail("NB-1", 2, total_aaa_milli=0, total_bbb_milli=0)
    b.ftail("F-NEG", batch_count=1, detail_count=2)
    return b


def scenario_three_decimals() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-MILLI")
    b.bhead("MB-1")
    b.detail(1, "MB-1", "BBB", 1)          # 0.001
    b.detail(2, "MB-1", "BBB", 2)          # 0.002 -> 0.003, no float drift
    b.btail("MB-1", 2, total_aaa_milli=0, total_bbb_milli=3)
    b.ftail("F-MILLI", batch_count=1, detail_count=2)
    return b


def scenario_seq_restart() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-SEQ")
    b.bhead("S1")
    b.detail(1, "S1", "AAA", 1000)
    b.detail(2, "S1", "AAA", 2000)
    b.btail("S1", 2, 3000, 0)
    b.bhead("S2")
    b.detail(1, "S2", "AAA", 5000)         # seq restarts at 1 per batch
    b.btail("S2", 1, 5000, 0)
    b.ftail("F-SEQ", batch_count=2, detail_count=3)
    return b


def scenario_duplicate_batch() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-DUP")
    b.bhead("DUP")
    b.detail(1, "DUP", "AAA", 1000)
    b.btail("DUP", 1, 1000, 0)
    b.bhead("DUP")                         # same batch_id twice
    b.detail(1, "DUP", "BBB", 2000)
    b.btail("DUP", 1, 0, 2000)
    b.ftail("F-DUP", batch_count=2, detail_count=2)
    return b


def scenario_orphan_quarantine() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-ORPHAN")
    b.truncated_line("quarantine line outside any batch")
    b.ftail("F-ORPHAN", batch_count=0, detail_count=0)
    return b


def scenario_data_after_ftail() -> HandoffBuilder:
    b = HandoffBuilder()
    b.fhead("F-AFTER")
    b.bhead("AB-1")
    b.btail("AB-1", 0, 0, 0)
    b.ftail("F-AFTER", batch_count=1, detail_count=0)
    b.detail(1, "AB-1", "AAA", 1000)       # illegal: nothing follows FTAIL
    return b


def scenario_line_gap() -> HandoffBuilder:
    """Evidence with a missing physical line: unverifiable, not rejected."""
    return scenario_handoff_good()         # the runner drops one JSONL row


def build_all(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = {
        "handoff_good": scenario_handoff_good(),
        "handoff_ftail_lied": scenario_handoff_ftail_lied(),
        "empty_batch": scenario_empty_batch(),
        "negative_offset": scenario_negative_offset(),
        "three_decimals": scenario_three_decimals(),
        "seq_restart": scenario_seq_restart(),
        "duplicate_batch": scenario_duplicate_batch(),
        "orphan_quarantine": scenario_orphan_quarantine(),
        "data_after_ftail": scenario_data_after_ftail(),
        "line_gap": scenario_line_gap(),
    }
    written = {}
    for name, builder in scenarios.items():
        path = out_dir / f"{name}.dat"
        builder.write(path)
        written[name] = path
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "data")
    for name, path in build_all(target).items():
        print(f"{name:22s} -> {path} ({path.stat().st_size} bytes)")
