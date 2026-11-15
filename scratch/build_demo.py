import sys, hashlib
sys.path.insert(0, '/workspace/copybook-decoder')
from tests.rewrite.fixtures import ref_zoned, ref_comp3, ref_text, sha256_hex

# ---- Record A (44 bytes, cp037) ----
a = bytearray(44)
def put(off, b):
    a[off:off+len(b)] = b

put(0, "ORD00001".encode("cp037"))                       # ORDER-ID X(8)
put(8, ref_text("ACME CORP", 12, "cp037"))              # CUSTOMER-NAME X(12)
# ITEM-GROUP occurs 2, each 8 bytes: CODE X(4), AMOUNT S9(5)V99 COMP-3 (4B)
put(20, "I001".encode("cp037"))
put(24, ref_comp3("123.45", 7, True))                   # 00 12 34 5C
put(28, "I002".encode("cp037"))
put(32, ref_comp3("-7.50", 7, True))                    # 00 00 07 5D
put(36, bytes([0x40, 0xFF]))                            # FILLER non-zero
# TOTAL S9(7)V99 COMP-3, 5 bytes: non-canonical legal positive sign nibble A,
# and a literal 0x0A byte embedded (would split a newline-based splitter).
put(38, bytes([0x00, 0x09, 0x0A, 0x87, 0x0A]))          # value 9087.00, sign A
put(43, "7".encode("cp037"))                            # FLAG
record_a = bytes(a)
print("record A len", len(record_a), record_a.hex())
print("record A sha", sha256_hex(record_a))

# ---- gap/preamble/trailing ----
preamble = b"HEAD\x00\x01"                              # 6 bytes
gap = b"\n\x00FEEDER-BYTES\n"                           # 14 bytes incl 0x0A
trailer = b"TAIL-END\x1a"                               # 9 bytes
print("len preamble", len(preamble), "gap", len(gap), "trailer", len(trailer))

# ---- Record B (50 bytes, cp500) ----
b = bytearray(50)
def putb(off, x):
    b[off:off+len(x)] = x
putb(0, "SUM001".encode("cp500") + " ".encode("cp500"))  # SUMMARY-ID X(6)
# BATCH-GROUP occurs 3, each 8: CODE X(2), AMT S9(9)V99 COMP-3 (6B)
putb(6, "B1".encode("cp500"))
putb(8, ref_comp3("100.00", 11, True))
putb(14, "B2".encode("cp500"))
putb(16, ref_comp3("-250.25", 11, True))
putb(22, "B3".encode("cp500"))
putb(24, ref_comp3("0.00", 11, True))
putb(30, bytes([0x5A, 0x7B, 0x40]))                     # FILLER non-zero cp500-ish
putb(33, ref_text("HELLO", 10, "cp500"))                # REMARK X(10)
putb(43, ref_zoned("-12.34", 7, True, "cp500"))         # ZONED-AMT S9(5)V99
record_b = bytes(b)
print("record B len", len(record_b), record_b.hex())
print("record B sha", sha256_hex(record_b))

whole = preamble + record_a + gap + record_b + trailer
print("total", len(whole))
print("whole sha", sha256_hex(whole))
print("record A span", len(preamble), len(preamble)+44)
print("record B span", len(preamble)+44+len(gap), len(preamble)+44+len(gap)+50)

# ---- target new bytes hand check ----
new_name = ref_text("ACME INC", 12, "cp037")
print("new CUSTOMER-NAME", new_name.hex(), "vs old", record_a[8:20].hex())
new_amt = ref_comp3("405.04", 7, True)
print("new ITEM-AMOUNT[0]", new_amt.hex())
print("as cp037 text repr", repr(new_amt.decode("cp037")))
new_flag = "9".encode("cp037")
print("new FLAG", new_flag.hex())
new_remark = ref_text("WORLD", 10, "cp500")
print("new REMARK", new_remark.hex(), "vs old", record_b[33:43].hex())
new_batch3 = ref_comp3("12.34", 11, True)
print("new BATCH-AMT[2]", new_batch3.hex())
new_zoned0 = ref_zoned("0.00", 7, True, "cp500")
print("new ZONED-AMT zero", new_zoned0.hex())

# 60 digit checks
w = "1234567890"*6
print("60-digit len", len(w))
pack = ref_comp3(w, 60, False)
packs = ref_comp3(w, 60, True)
print("wide pack unsigned", len(pack), pack.hex())
print("wide pack signed  ", len(packs), packs.hex())
disp = bytes(0xF0|int(d) for d in w)
print("wide disp unsigned", len(disp), disp.hex())
disps = bytearray(0xF0|int(d) for d in w); disps[-1]=0xC0|int(w[-1])
print("wide disp signed  ", len(bytes(disps)), bytes(disps).hex())

# scaled decimal tests
print("scaled comp3 12.3 ->", ref_comp3("12.3", 9, True).hex())   # S9(7)V99 scaled=1230
print("scaled zoned 12.34 ->", ref_zoned("12.34",7,True).hex())
print("scaled zoned neg zero ->", ref_zoned("-0",7,True).hex())
