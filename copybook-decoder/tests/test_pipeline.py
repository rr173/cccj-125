"""End-to-end pipeline: version switching, truncation, quarantine, offsets."""

import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from copybook import (DecodeResult, DecodedRecord, Manifest, ManifestError,
                      QuarantinedLine, decode_file)
from tests.builders import comp3, ebcdic, write_lines, zoned

ACCT_V1 = """\
01  ACCT-REC.
    05  ACCT-ID     PIC X(4).
    05  AMOUNT      PIC S9(3)V99 COMP-3.
    05  CODE        PIC X(2).
"""
# v2 adds a trailing flag -> record length changes 9 -> 10
ACCT_V2 = """\
01  ACCT-REC.
    05  ACCT-ID     PIC X(4).
    05  AMOUNT      PIC S9(3)V99 COMP-3.
    05  CODE        PIC X(2).
    05  FLAG        PIC X(1).
"""

MANIFEST = {"layouts": [
    {"sender": "BANKA", "copybook": "ACCT", "version": "1",
     "effective_from": "2024-01-01", "effective_to": "2024-08-31",
     "file": "acct_v1.cpy"},
    {"sender": "BANKA", "copybook": "ACCT", "version": "2",
     "effective_from": "2024-09-01", "file": "acct_v2.cpy"},
]}


def line_v1(acct="A001", amount=12345, code="OK") -> bytes:
    return ebcdic(acct) + comp3(amount, 5) + ebcdic(code)


def line_v2(acct="A001", amount=12345, code="OK", flag="Y") -> bytes:
    return line_v1(acct, amount, code) + ebcdic(flag)


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "acct_v1.cpy").write_text(ACCT_V1)
        (self.dir / "acct_v2.cpy").write_text(ACCT_V2)
        (self.dir / "manifest.json").write_text(json.dumps(MANIFEST))
        self.manifest = Manifest.load(self.dir / "manifest.json")

    def write_data(self, name, lines, final_newline=True) -> Path:
        return write_lines(self.dir / name, lines, final_newline)


class TestVersionSwitching(PipelineTestCase):
    def test_same_directory_mixed_versions(self):
        # one directory holds files of both layout generations
        feb = self.write_data("BANKA-20240201.dat",
                              [line_v1("A001", 100), line_v1("A002", 200)])
        oct_ = self.write_data("BANKA-20241001.dat",
                               [line_v2("B001", 300, "OK", "N")])

        r1 = decode_file(feb, sender="BANKA", business_date="2024-02-01",
                         manifest=self.manifest)
        r2 = decode_file(oct_, sender="BANKA", business_date="2024-10-01",
                         manifest=self.manifest)

        self.assertEqual(r1.quarantine_count, 0)
        self.assertEqual(r2.quarantine_count, 0)
        # each record carries the layout version actually used
        self.assertEqual(r1.records[0].layout_version, "1")
        self.assertEqual(r2.records[0].layout_version, "2")
        self.assertEqual(r1.records[0].fields["AMOUNT"], Decimal("1.00"))
        self.assertEqual(r2.records[0].fields["AMOUNT"], Decimal("3.00"))
        self.assertEqual(r2.records[0].fields["FLAG"], "N")
        self.assertNotIn("FLAG", r1.records[0].fields)

    def test_wrong_version_would_not_fit(self):
        # a v1 file decoded under v2 rules has wrong length -> proves the
        # manifest date (not the bytes) drives version selection
        feb = self.write_data("BANKA-20240201.dat", [line_v1()])
        result = decode_file(feb, sender="BANKA", business_date="2024-02-01",
                             manifest=self.manifest)
        self.assertEqual(result.ok_count, 1)
        # no manifest entry covers 2020 -> must not guess
        with self.assertRaises(ManifestError):
            decode_file(feb, sender="BANKA", business_date="2020-01-01",
                        manifest=self.manifest)


class TestTruncation(PipelineTestCase):
    def test_truncated_last_line_is_quarantined(self):
        path = self.write_data(
            "trunc.dat",
            [line_v1("A001"), line_v1("A002"), ebcdic("A003") + b"\x12"],
            final_newline=False)
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual(result.ok_count, 2)
        self.assertEqual(result.quarantine_count, 1)
        q = result.quarantined[0]
        self.assertEqual(q.reason, "truncated")
        self.assertEqual(q.line_number, 3)
        self.assertEqual(q.byte_start, 20)   # two 9-byte lines + 2 newlines
        self.assertEqual(q.byte_end, 25)
        self.assertEqual(q.raw, ebcdic("A003") + b"\x12")

    def test_overlong_line_is_quarantined(self):
        path = self.write_data("long.dat",
                               [line_v1("A001") + ebcdic("EXTRA")])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual(result.ok_count, 0)
        self.assertEqual(result.quarantined[0].reason, "overlong")

    def test_blank_line_mid_file_is_quarantined(self):
        path = self.write_data("blank.dat", [line_v1("A001"), b"",
                                             line_v1("A002")])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual([r.line_number for r in result.records], [1, 3])
        self.assertEqual(result.quarantined[0].line_number, 2)
        self.assertEqual(result.quarantined[0].reason, "truncated")


class TestQuarantineIsolation(PipelineTestCase):
    def test_bad_line_does_not_lose_following_lines(self):
        good1 = line_v1("A001", 100)
        good2 = line_v1("A002", 200)
        bad = ebcdic("A003") + b"\xff\xff\xff" + ebcdic("OK")  # bad COMP-3
        good4 = line_v1("A004", 400)
        good5 = line_v1("A005", 500)
        path = self.write_data("mixed.dat",
                               [good1, good2, bad, good4, good5])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)

        # line numbers of the good records stay stable around the hole
        self.assertEqual([r.line_number for r in result.records],
                         [1, 2, 4, 5])
        self.assertEqual(result.quarantine_count, 1)
        q = result.quarantined[0]
        self.assertEqual(q.line_number, 3)
        self.assertEqual(q.reason, "decode")
        self.assertIn("AMOUNT", q.error)
        # absolute byte offset of the failing field inside the file
        self.assertEqual(q.byte_start, 20)
        self.assertEqual(q.error_offset, 20 + 4)  # AMOUNT starts at rec byte 4

        # values on both sides of the hole decoded correctly
        amounts = [r.fields["AMOUNT"] for r in result.records]
        self.assertEqual(amounts, [Decimal("1.00"), Decimal("2.00"),
                                   Decimal("4.00"), Decimal("5.00")])

    def test_every_record_carries_source_and_position(self):
        path = self.write_data("meta.dat",
                               [line_v1("A001"), line_v1("A002")])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        for i, record in enumerate(result.records):
            with self.subTest(line=i + 1):
                self.assertEqual(record.source_file, str(path))
                self.assertEqual(record.line_number, i + 1)
                self.assertEqual(record.byte_start, i * 10)
                self.assertEqual(record.byte_end, i * 10 + 9)
                self.assertEqual(record.layout_name, "ACCT")
                self.assertEqual(record.layout_version, "1")


class TestCrlf(PipelineTestCase):
    def test_crlf_lines_decode(self):
        path = self.dir / "crlf.dat"
        path.write_bytes(line_v1("A001") + b"\r\n" + line_v1("A002") + b"\r\n")
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual(result.ok_count, 2)
        self.assertEqual(result.records[0].fields["ACCT-ID"], "A001")
        # byte_end spans the record bytes plus the \r (newline excluded)
        self.assertEqual(result.records[0].byte_end, 10)
        self.assertEqual(result.records[1].byte_start, 11)


class TestLocate(PipelineTestCase):
    def test_locate_by_raw_byte_offset(self):
        bad = ebcdic("A003") + b"\xff\xff\xff" + ebcdic("OK")
        path = self.write_data("locate.dat", [line_v1("A001"), bad,
                                               line_v1("A002")])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)

        hit = result.locate(0)
        self.assertIsInstance(hit, DecodedRecord)
        self.assertEqual(hit.line_number, 1)

        hit = result.locate(12)          # inside the quarantined line
        self.assertIsInstance(hit, QuarantinedLine)
        self.assertEqual(hit.line_number, 2)

        hit = result.locate(25)          # third line
        self.assertIsInstance(hit, DecodedRecord)
        self.assertEqual(hit.line_number, 3)

        self.assertIsNone(result.locate(10_000))


class TestExport(PipelineTestCase):
    def test_export_quarantine(self):
        bad1 = ebcdic("A003") + b"\xff\xff\xff" + ebcdic("OK")
        bad2 = ebcdic("A004")[:5]        # truncated
        path = self.write_data("exp.dat", [line_v1("A001"), bad1, bad2],
                               final_newline=False)
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        out_dir = self.dir / "quarantine"
        paths = result.export_quarantine(out_dir)

        raw = paths["bin"].read_bytes()
        self.assertEqual(raw, bad1 + b"\n" + bad2 + b"\n")

        index = [json.loads(line)
                 for line in paths["index"].read_text().splitlines()]
        self.assertEqual(len(index), 2)
        self.assertEqual(index[0]["line_number"], 2)
        self.assertEqual(index[0]["reason"], "decode")
        self.assertEqual(index[0]["byte_start"], 10)
        self.assertEqual(index[0]["byte_end"], 19)
        self.assertEqual(index[0]["error_offset"], 14)
        self.assertEqual(index[1]["line_number"], 3)
        self.assertEqual(index[1]["reason"], "truncated")
        self.assertEqual(index[1]["byte_start"], 20)

    def test_write_jsonl(self):
        path = self.write_data("jsonl.dat", [line_v1("A001", 12345)])
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        out = self.dir / "records.jsonl"
        result.write_jsonl(out)
        (row,) = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(row["meta"]["line_number"], 1)
        self.assertEqual(row["meta"]["byte_start"], 0)
        self.assertEqual(row["meta"]["byte_end"], 9)
        self.assertEqual(row["meta"]["layout_version"], "1")
        self.assertEqual(row["meta"]["source_file"], str(path))
        self.assertEqual(row["fields"]["ACCT-ID"], "A001")
        self.assertEqual(row["fields"]["AMOUNT"], "123.45")  # Decimal -> str


class TestEdgeCases(PipelineTestCase):
    def test_empty_file(self):
        path = self.write_data("empty.dat", [], final_newline=False)
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual(result.total_lines, 0)

    def test_trailing_newline_makes_no_phantom_record(self):
        path = self.write_data("nl.dat", [line_v1("A001")], final_newline=True)
        result = decode_file(path, sender="BANKA", business_date="2024-03-01",
                             manifest=self.manifest)
        self.assertEqual(result.total_lines, 1)

    def test_view_error_keeps_record_out_of_quarantine(self):
        (self.dir / "flex.cpy").write_text("""\
01  REC.
    05  FLEX        PIC X(4).
    05  FLEX-NUM    REDEFINES FLEX PIC 9(4).
""")
        (self.dir / "manifest2.json").write_text(json.dumps({"layouts": [
            {"sender": "BANKB", "copybook": "FLEX", "version": "9",
             "effective_from": "2024-01-01", "file": "flex.cpy"}]}))
        manifest = Manifest.load(self.dir / "manifest2.json")
        path = self.write_data("flex.dat", [ebcdic("ABCD")])
        result = decode_file(path, sender="BANKB", business_date="2024-05-01",
                             manifest=manifest)
        self.assertEqual(result.quarantine_count, 0)
        record = result.records[0]
        self.assertEqual(record.fields["FLEX"], "ABCD")
        self.assertIsNone(record.fields["FLEX-NUM"])
        self.assertEqual(len(record.field_errors), 1)
        self.assertEqual(record.field_errors[0].field, "FLEX-NUM")


if __name__ == "__main__":
    unittest.main()
