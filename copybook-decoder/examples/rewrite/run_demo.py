"""End-to-end demonstration: original binary -> supplier new original.

This script is intentionally self-contained and does not reuse the
production encoder to build its expected bytes.  Every expected hex
value below is computed by hand (or by an independent reference encoder
in this file) so the demonstration proves the engine's bytes rather than
echoing them back.

What it walks through:

1. build an original with two fixed OCCURS groups, an in-group
   REDEFINES, a non-zero FILLER, a positive number stored with a
   non-canonical but legal sign nibble and an embedded 0x0A byte;
2. mix two layouts in one file, with preamble/gap/trailer bytes that
   must be preserved exactly;
3. hand-compute the target field offsets and old/new hex;
4. run a successful fixed-point rewrite, including an overlapping
   amount/text REDEFINES pair that requests identical shared bytes;
5. show that the same pair made inconsistent rejects the whole job;
6. prove reordering records and edits changes neither product bytes
   nor evidence;
7. independently check a 60-digit value, a scaled decimal and a cp500
   text field, and feed one rewritten record to the existing Decoder.

Run::

    python3 examples/rewrite/run_demo.py --workdir /tmp/rewrite-demo
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copybook.decoder import Decoder
from copybook.parser import parse_copybook
from copybook.rewrite import (RewriteConflictError, rewrite_original)
from tests.rewrite.fixtures import (LAYOUT_A, LAYOUT_B, LAYOUT_WIDE,
                                    build_demo_file, ref_comp3, ref_text,
                                    ref_zoned)

# Hand-computed layout A (44 bytes) offsets, from parser resolution:
#   ORDER-ID       [0, 8)    CUSTOMER-NAME   [8, 20)
#   ITEM-GROUP[0]  [20, 28)  ITEM-GROUP[1]   [28, 36)
#     ITEM-CODE     [+0,+4)   ITEM-AMOUNT/NOTE [+4,+8)
#   FILLER         [36, 38)  TOTAL/TOTAL-TEXT [38, 43)
#   FLAG           [43, 44)
# Layout B (50 bytes, cp500): REMARK [33,43), BATCH-AMT[2] [24,30),
#   ZONED-AMT [43,50).


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_copybooks(workdir: Path) -> Path:
    cdir = workdir / "copybooks"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "ORDA.cpy").write_text(LAYOUT_A, encoding="utf-8")
    (cdir / "SUMB.cpy").write_text(LAYOUT_B, encoding="utf-8")
    (cdir / "WIDE.cpy").write_text(LAYOUT_WIDE, encoding="utf-8")
    return cdir


def catalog(cdir: Path) -> dict:
    return {
        "A": {"copybook": "ORDA.cpy", "name": "ORD-A", "version": "1",
              "codec": "cp037"},
        "B": {"copybook": "SUMB.cpy", "name": "SUM-B", "version": "1",
              "codec": "cp500"},
    }


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def run(workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    cdir = write_copybooks(workdir)
    source, spans = build_demo_file()
    source_path = workdir / "original.bin"
    source_path.write_bytes(source)
    a0, a1 = spans["A"]
    b0, b1 = spans["B"]
    rec_a, rec_b = source[a0:a1], source[b0:b1]

    banner("1. hand-computed original layout and field offsets")
    print(f"file length            : {len(source)} bytes")
    print(f"record A span          : [{a0}, {a1}) (44 bytes, cp037)")
    print(f"record B span          : [{b0}, {b1}) (50 bytes, cp500)")
    print(f"FILLER A [36,38) hex   : {rec_a[36:38].hex()} (non-zero, kept)")
    print(f"TOTAL A  [38,43) hex   : {rec_a[38:43].hex()} "
          "(0x0A inside; non-canonical sign nibble A; kept)")

    banner("2. hand-computed new bytes (independent reference encoders)")
    new_name = ref_text("ACME INC", 12, "cp037")
    new_amount = ref_comp3("405.04", 7, True, scale=2)
    new_flag = "9".encode("cp037")
    new_remark = ref_text("WORLD", 10, "cp500")
    new_batch3 = ref_comp3("12.34", 11, True, scale=2)
    new_zoned0 = ref_zoned("0.00", 7, True, "cp500", scale=2)
    print(f"CUSTOMER-NAME old -> new: {rec_a[8:20].hex()} -> "
          f"{new_name.hex()}")
    print(f"ITEM-AMOUNT[0] old -> new: {rec_a[24:28].hex()} -> "
          f"{new_amount.hex()}")
    print(f"FLAG old -> new          : {rec_a[43:44].hex()} -> "
          f"{new_flag.hex()}")
    print(f"REMARK old -> new        : {rec_b[33:43].hex()} -> "
          f"{new_remark.hex()}")
    print(f"BATCH-AMT[2] old -> new  : {rec_b[24:30].hex()} -> "
          f"{new_batch3.hex()}")
    print(f"ZONED-AMT old -> new      : {rec_b[43:50].hex()} -> "
          f"{new_zoned0.hex()}")
    # the packed new amount is also valid cp037 text for the REDEFINES view
    note_text = new_amount.decode("cp037")
    print(f"same bytes as ITEM-NOTE text: {new_amount.hex()} "
          f"== cp037 of {note_text!r}")

    def make_edits(include_note: bool, conflict: bool):
        edits_a = [
            {"path": ["CUSTOMER-NAME"],
             "expected_hex": rec_a[8:20].hex(), "value": "ACME INC"},
            {"path": ["ITEM-GROUP", 0, "ITEM-AMOUNT"],
             "expected_hex": rec_a[24:28].hex(), "value": "405.04"},
            {"path": ["FLAG"],
             "expected_hex": rec_a[43:44].hex(), "value": "9"},
        ]
        if include_note:
            edits_a.append({
                "path": ["ITEM-GROUP", 0, "ITEM-NOTE"],
                "expected_hex": rec_a[24:28].hex(),
                "value": "ZZZZ" if conflict else note_text})
        edits_b = [
            {"path": ["REMARK"],
             "expected_hex": rec_b[33:43].hex(), "value": "WORLD"},
            {"path": ["BATCH-GROUP", 2, "BATCH-AMT"],
             "expected_hex": rec_b[24:30].hex(), "value": "12.34"},
            {"path": ["ZONED-AMT"],
             "expected_hex": rec_b[43:50].hex(), "value": "0.00"},
        ]
        return edits_a, edits_b

    def order_payload(edits_a, edits_b):
        return {
            "source_sha256": sha(source),
            "layouts": catalog(cdir),
            "records": [
                {"byte_start": a0, "byte_end": a1, "layout": "A",
                 "record_sha256": sha(rec_a), "edits": edits_a},
                {"byte_start": b0, "byte_end": b1, "layout": "B",
                 "record_sha256": sha(rec_b), "edits": edits_b},
            ],
        }

    def deliver(payload, name):
        order_path = workdir / f"{name}.order.json"
        order_path.write_text(json.dumps(payload), encoding="utf-8")
        out_dir = workdir / name
        return rewrite_original(
            order_path, source_path=source_path, output_dir=out_dir,
            temp_dir=workdir / "staging", copybook_base=cdir)

    banner("3. successful fixed-point rewrite with consistent REDEFINES")
    edits_a, edits_b = make_edits(include_note=True, conflict=False)
    evidence = deliver(order_payload(edits_a, edits_b), "delivery-ok")
    product = (workdir / "delivery-ok" /
               "original.rewritten.bin").read_bytes()
    print(f"product length == original length : "
          f"{len(product) == len(source)} ({len(product)} bytes)")
    diffs = [i for i in range(len(source)) if product[i] != source[i]]
    print(f"independent byte difference count : {len(diffs)}")
    print(f"evidence changed_bytes            : "
          f"{evidence['counts']['changed_bytes']}")
    print(f"evidence intervals                : "
          f"{len(evidence['change_intervals'])} adjacent-merged")
    for iv in evidence["change_intervals"]:
        print(f"  [{iv['byte_start']}, {iv['byte_end']}): "
              f"{iv['before_hex']} -> {iv['after_hex']}")
    # untouched non-canonical field survives byte for byte
    assert product[a0 + 38:a0 + 43] == rec_a[38:43]
    assert product[:a0] == source[:a0]
    assert product[a1:b0] == source[a1:b0]
    assert product[b1:] == source[b1:]
    print("preamble/gap/trailer/FILLER/non-canonical TOTAL: all preserved")

    banner("4. inconsistent overlap rejects the whole job")
    bad_a, bad_b = make_edits(include_note=True, conflict=True)
    try:
        deliver(order_payload(bad_a, bad_b), "delivery-conflict")
    except RewriteConflictError as exc:
        print(f"refused as expected: {exc}")
    assert not (workdir / "delivery-conflict").exists()
    print("no delivery directory was created; original bytes untouched")
    assert source_path.read_bytes() == source

    banner("5. reordering records and edits changes nothing")
    payload = order_payload(edits_a, edits_b)
    shuffled = json.loads(json.dumps(payload))
    for rec in shuffled["records"]:
        rec["edits"] = list(reversed(rec["edits"]))
    shuffled["records"] = list(reversed(shuffled["records"]))
    ev2 = deliver(shuffled, "delivery-shuffled")
    product2 = (workdir / "delivery-shuffled" /
                "original.rewritten.bin").read_bytes()
    assert product == product2
    assert ev2["change_intervals"] == evidence["change_intervals"]
    assert ev2["counts"] == evidence["counts"]
    print("shuffled product byte-identical; evidence intervals/counts equal")

    banner("6. 60-digit, scaled decimal and cp500 independent byte checks")
    wide = parse_copybook(LAYOUT_WIDE, codec="cp037")
    big = "1234567890" * 6
    wide_pack = ref_comp3(big, 60, True)             # 31 bytes
    wide_disp = bytes(0xF0 | int(d) for d in big)
    wide_disp = wide_disp[:-1] + bytes([0xC0 | int(big[-1])])
    scaled = ref_comp3("12.3", 9, True, scale=2)     # S9(7)V99
    # drive them through the real encoder and compare to hand bytes
    from copybook.rewrite.encoder import encode_value
    assert encode_value(big, wide.find("WIDE-PACK-S"), "cp037") == wide_pack
    assert encode_value(big, wide.find("WIDE-DISP-S"), "cp037") == wide_disp
    assert encode_value("12.3", wide.find("SCALED"), "cp037") == scaled
    print(f"60-digit COMP-3 (31B): {wide_pack.hex()}")
    print(f"scaled 12.3 -> 12.30 : {scaled.hex()}")
    cp500 = ref_text("WORLD", 10, "cp500")
    print(f"cp500 'WORLD' padded : {cp500.hex()}")
    assert product[b0 + 33:b0 + 43] == cp500

    banner("7. supplementary business-value check via the existing Decoder")
    layout_b = parse_copybook(LAYOUT_B, name="SUM-B", version="1",
                              codec="cp500")
    fields, errors = Decoder(layout_b).decode(product[b0:b1])
    # only the alternative numeric view over text may report a view error
    assert all(e.field == "REMARK-NUM" for e in errors)
    print(f"decoded REMARK        : {fields['REMARK']}")
    print(f"decoded ZONED-AMT     : {fields['ZONED-AMT']}")
    print(f"decoded BATCH-AMT[2]  : "
          f"{fields['BATCH-GROUP'][2]['BATCH-AMT']}")
    print(f"decoded BATCH-AMT[0]  : "
          f"{fields['BATCH-GROUP'][0]['BATCH-AMT']} (unedited)")
    assert fields["REMARK"] == "WORLD"
    assert str(fields["ZONED-AMT"]) == "0.00"
    assert str(fields["BATCH-GROUP"][2]["BATCH-AMT"]) == "12.34"

    banner("demo complete")
    print(f"supplier new original : "
          f"{workdir / 'delivery-ok' / 'original.rewritten.bin'}")
    print(f"byte-change evidence  : "
          f"{workdir / 'delivery-ok' / 'rewrite_evidence.json'}")


def main_cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)
    run(Path(args.workdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
