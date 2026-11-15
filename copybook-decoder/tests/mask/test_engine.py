"""End-to-end engine tests for the masking generator."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copybook.mask import (MaskConflictError, MaskError, MaskInputError,
                           MaskKey, MaskPolicy, MaskPolicyError, run_mask)

KEY = MaskKey._from_text("unit-test-key-material-0123456789", "test")


def _rec(source, line, fields, layout="L"):
    return {"meta": {"source_file": source, "line_number": line,
                     "byte_start": line * 100, "byte_end": line * 100 + 90,
                     "layout": layout, "layout_version": "1"},
            "fields": fields}


def _write_jsonl(path: Path, rows: list) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _policy(fields):
    return MaskPolicy.from_dict({"fields": fields})


def _rule(path, action, kind, **kw):
    rule = {"path": path, "action": action, "kind": kind}
    rule.update(kw)
    return rule


class MaskTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mask-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _inputs(self, name, rows):
        return _write_jsonl(self.tmp / name, rows)

    def _out(self, name="out"):
        return self.tmp / name

    def _read_jsonl(self, path):
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()]


class TestHappyPath(MaskTestCase):

    def test_cross_file_join_and_counts(self):
        a = self._inputs("accounts.jsonl", [
            _rec("a.dat", 1, {"ACCT": "1001", "NAME": "ZHANG WEI"}),
            _rec("a.dat", 2, {"ACCT": "1002", "NAME": "LI NA"}),
        ])
        t = self._inputs("txns.jsonl", [
            _rec("t.dat", 1, {"ACCT": "1001", "AMT": "10.00"}),
            _rec("t.dat", 2, {"ACCT": "1001", "AMT": "20.00"}),
            _rec("t.dat", 3, {"ACCT": "1003", "AMT": "30.00"}),
        ])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="acct"),
            _rule(["NAME"], "synthesize", "name"),
        ])
        summary = run_mask([t, a], policy=policy, key=KEY,
                           output_dir=self._out())
        self.assertEqual(summary["conclusion"], "masked")
        self.assertEqual(summary["records_in"], 5)
        self.assertEqual(summary["records_out"], 5)
        self.assertEqual(summary["records_rejected"], 0)
        self.assertEqual(summary["distinct_pseudonym_values"], 3)

        accounts = self._read_jsonl(self._out() / "accounts.masked.jsonl")
        txns = self._read_jsonl(self._out() / "txns.masked.jsonl")
        fake_1001 = accounts[0]["fields"]["ACCT"]
        self.assertEqual(txns[0]["fields"]["ACCT"], fake_1001)
        self.assertEqual(txns[1]["fields"]["ACCT"], fake_1001)
        self.assertNotEqual(accounts[1]["fields"]["ACCT"], fake_1001)
        self.assertNotEqual(txns[2]["fields"]["ACCT"], fake_1001)
        self.assertEqual(len(fake_1001), 4)          # format preserved
        self.assertTrue(fake_1001.isdigit())
        # meta passes through unchanged
        self.assertEqual(accounts[0]["meta"]["source_file"], "a.dat")
        self.assertEqual(accounts[0]["meta"]["byte_start"], 100)

    def test_null_vs_missing(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1", "NAME": None}),
            _rec("f.dat", 2, {"ACCT": "2"}),
            _rec("f.dat", 3, {"ACCT": "3", "NAME": "REAL NAME"}),
        ])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["NAME"], "synthesize", "name"),
        ])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        rows = self._read_jsonl(self._out() / "f.masked.jsonl")
        self.assertIsNone(rows[0]["fields"]["NAME"])       # null kept
        self.assertNotIn("NAME", rows[1]["fields"])        # missing stays
        self.assertNotEqual(rows[2]["fields"]["NAME"], "REAL NAME")

        report = json.loads((self._out() / "mask_report.json")
                            .read_text(encoding="utf-8"))
        name_rule = [r for r in report["policy"]["rules"]
                     if r["path_text"] == "NAME"][0]
        self.assertEqual(name_rule["nulls"], 1)
        self.assertEqual(name_rule["missing_records"], 1)
        self.assertEqual(name_rule["matched_records"], 2)

    def test_on_null_and_on_empty_reject(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1", "ALT": None}),
            _rec("f.dat", 2, {"ACCT": "2", "ALT": ""}),
            _rec("f.dat", 3, {"ACCT": "3", "ALT": "9"}),
        ])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["ALT"], "pseudonym", "account", group="g2",
                  on_null="reject", on_empty="reject"),
        ])
        summary = run_mask([f], policy=policy, key=KEY,
                           output_dir=self._out())
        self.assertEqual(summary["records_out"], 1)
        self.assertEqual(summary["records_rejected"], 2)
        report = json.loads((self._out() / "mask_report.json")
                            .read_text(encoding="utf-8"))
        reasons = {(r["line_number"], "null" in r["reason"] and "null"
                    or "empty") for r in report["rejections"]}
        self.assertEqual(reasons, {(1, "null"), (2, "empty")})
        rows = self._read_jsonl(self._out() / "f.masked.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["line_number"], 3)

    def test_unlisted_fields_pass_through(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1", "KEEP": "RAW", "NUM": 42}),
        ])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        row = self._read_jsonl(self._out() / "f.masked.jsonl")[0]
        self.assertEqual(row["fields"]["KEEP"], "RAW")
        self.assertEqual(row["fields"]["NUM"], 42)

    def test_occurs_list_wildcard(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1", "TXNS": [
                {"AMT": "10.00"}, {"AMT": "20.00"}, {"NOTE": "x"}]}),
        ])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["TXNS", "AMT"], "shift", "amount", max_pct="5"),
        ])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        row = self._read_jsonl(self._out() / "f.masked.jsonl")[0]
        amts = [el.get("AMT") for el in row["fields"]["TXNS"]]
        self.assertNotEqual(amts[0], "10.00")
        self.assertNotEqual(amts[1], "20.00")
        self.assertIsNone(amts[2])                    # element lacks AMT
        self.assertEqual(row["fields"]["TXNS"][2]["NOTE"], "x")
        report = json.loads((self._out() / "mask_report.json")
                            .read_text(encoding="utf-8"))
        amt_rule = [r for r in report["policy"]["rules"]
                    if r["path_text"] == "TXNS/AMT"][0]
        self.assertEqual(amt_rule["matched_records"], 1)
        self.assertEqual(amt_rule["matched_values"], 2)

    def test_integer_account_type_preserved(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": 100234}),
            _rec("f.dat", 2, {"ACCT": 100235}),
        ])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        rows = self._read_jsonl(self._out() / "f.masked.jsonl")
        for row in rows:
            fake = row["fields"]["ACCT"]
            self.assertIsInstance(fake, int)
            self.assertEqual(len(str(fake)), 6)       # no leading zero
        self.assertNotEqual(rows[0]["fields"]["ACCT"],
                            rows[1]["fields"]["ACCT"])

    def test_report_and_record_report_shape(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1"}),
            _rec("f.dat", 2, {"ACCT": "2"}),
        ])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        report = json.loads((self._out() / "mask_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["report"], "mask-run")
        self.assertEqual(report["conclusion"], "masked")
        self.assertTrue(report["key_fingerprint"].startswith("sha256:"))
        self.assertEqual(report["policy"]["groups"][0]["distinct_values"],
                         2)
        self.assertEqual(report["policy"]["groups"][0]
                         ["collision_check"], "passed")
        self.assertTrue(report["notes"])
        records = self._read_jsonl(self._out() / "record_report.jsonl")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["status"], "masked")
        self.assertEqual(records[0]["outcomes"][0]["result"], "pseudonym")
        # no original values anywhere in the reports
        blob = (self._out() / "mask_report.json").read_text() + \
            (self._out() / "record_report.jsonl").read_text()
        self.assertNotIn('"1"', blob.replace('"ACCT"', ""))
        text_report = (self._out() / "mask_report.txt") \
            .read_text(encoding="utf-8")
        self.assertIn("脱敏样本生成报告", text_report)


class TestDeterminism(MaskTestCase):

    def _build(self):
        a = self._inputs("a.jsonl", [
            _rec("a.dat", i, {"ACCT": f"100{i}", "AMT": f"{i}.00"})
            for i in range(1, 8)])
        b = self._inputs("b.jsonl", [
            _rec("b.dat", i, {"ACCT": f"100{i}", "AMT": f"{i}.50"})
            for i in range(1, 8)])
        return a, b

    def _policy(self):
        return _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["AMT"], "shift", "amount", max_pct="5"),
        ])

    def test_order_and_chunk_invariance(self):
        a, b = self._build()
        run_mask([a, b], policy=self._policy(), key=KEY,
                 output_dir=self._out("o1"), chunk_size=3)
        run_mask([b, a], policy=self._policy(), key=KEY,
                 output_dir=self._out("o2"), chunk_size=1)
        for name in ("a.masked.jsonl", "b.masked.jsonl",
                     "mask_report.json", "record_report.jsonl",
                     "mask_report.txt"):
            self.assertEqual((self._out("o1") / name).read_bytes(),
                             (self._out("o2") / name).read_bytes(),
                             f"{name} differs between runs")

    def test_key_rotation_changes_mapping(self):
        a, _ = self._build()
        other = MaskKey._from_text("rotated-key-material-aaaaaaaa", "t")
        run_mask([a], policy=self._policy(), key=KEY,
                 output_dir=self._out("o1"))
        run_mask([a], policy=self._policy(), key=other,
                 output_dir=self._out("o2"))
        before = self._read_jsonl(self._out("o1") / "a.masked.jsonl")
        after = self._read_jsonl(self._out("o2") / "a.masked.jsonl")
        self.assertNotEqual([r["fields"]["ACCT"] for r in before],
                            [r["fields"]["ACCT"] for r in after])


class TestFailures(MaskTestCase):

    def test_path_never_matched(self):
        f = self._inputs("f.jsonl", [_rec("f.dat", 1, {"ACCT": "1"})])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["NOPE"], "synthesize", "name"),
        ])
        with self.assertRaisesRegex(MaskPolicyError, "never matched"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_container_path_rejected(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1", "GRP": {"INNER": "x"}})])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["GRP"], "synthesize", "name"),
        ])
        with self.assertRaisesRegex(MaskPolicyError, "container"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_scalar_crossing_rejected(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1"})])
        policy = _policy([_rule(["ACCT", "SUB"], "synthesize", "name")])
        with self.assertRaisesRegex(MaskPolicyError, "scalar"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())

    def test_type_mismatch_amount_on_name(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"NAME": "ZHANG WEI"})])
        policy = _policy([_rule(["NAME"], "shift", "amount",
                                max_pct="5")])
        with self.assertRaisesRegex(MaskPolicyError, "decimal"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_type_mismatch_bad_date(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"D": "15/01/2024"})])
        policy = _policy([_rule(["D"], "shift", "date", max_days=5)])
        with self.assertRaisesRegex(MaskPolicyError, "does not match"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())

    def test_type_mismatch_pseudonym_on_float(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": 1.5})])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskPolicyError, "pseudonym"):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())

    def test_mixed_type_group_rejected(self):
        a = self._inputs("a.jsonl", [_rec("a.dat", 1, {"ACCT": "1001"})])
        b = self._inputs("b.jsonl", [_rec("b.dat", 1, {"ACCT": 1001})])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskPolicyError, "mix"):
            run_mask([a, b], policy=policy, key=KEY,
                     output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_mapping_conflict_aborts(self):
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": "1001"}),
            _rec("f.dat", 2, {"ACCT": "1002"}),
        ])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with mock.patch("copybook.mask.engine.pseudonym_value",
                        lambda *a, **kw: "9999"):
            with self.assertRaisesRegex(MaskConflictError, "collapse"):
                run_mask([f], policy=policy, key=KEY,
                         output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_corrupt_json(self):
        path = self.tmp / "bad.jsonl"
        path.write_text(json.dumps(_rec("f.dat", 1, {"ACCT": "1"}))
                        + "\n{\"meta\": broken\n", encoding="utf-8")
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskInputError, "invalid JSON"):
            run_mask([path], policy=policy, key=KEY,
                     output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_missing_meta_or_fields(self):
        for broken in ({"fields": {"ACCT": "1"}},
                       {"meta": {}, "fields": {"ACCT": "1"}},
                       {"meta": {"source_file": "x", "line_number": 1}},
                       ["not", "an", "object"]):
            path = self.tmp / "broken.jsonl"
            path.write_text(json.dumps(broken) + "\n", encoding="utf-8")
            policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                    group="g")])
            with self.assertRaises(MaskInputError, msg=str(broken)):
                run_mask([path], policy=policy, key=KEY,
                         output_dir=self._out())
            self.assertFalse(self._out().exists())

    def test_existing_output_dir_untouched(self):
        f = self._inputs("f.jsonl", [_rec("f.dat", 1, {"ACCT": "1"})])
        out = self._out()
        out.mkdir()
        marker = out / "marker.txt"
        marker.write_text("keep me", encoding="utf-8")
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskError, "already exists"):
            run_mask([f], policy=policy, key=KEY, output_dir=out)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")

    def test_key_fingerprint_enforced(self):
        f = self._inputs("f.jsonl", [_rec("f.dat", 1, {"ACCT": "1"})])
        policy = MaskPolicy.from_dict({
            "key_fingerprint": "sha256:0000000000000000",
            "fields": [_rule(["ACCT"], "pseudonym", "account",
                             group="g")],
        })
        from copybook.mask import MaskKeyError
        with self.assertRaises(MaskKeyError):
            run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        self.assertFalse(self._out().exists())

    def test_input_validation(self):
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskInputError, "at least one"):
            run_mask([], policy=policy, key=KEY, output_dir=self._out())
        with self.assertRaisesRegex(MaskInputError, "does not exist"):
            run_mask([self.tmp / "nope.jsonl"], policy=policy, key=KEY,
                     output_dir=self._out())
        f = self._inputs("f.jsonl", [_rec("f.dat", 1, {"ACCT": "1"})])
        with self.assertRaisesRegex(MaskInputError, "duplicate"):
            run_mask([f, f], policy=policy, key=KEY,
                     output_dir=self._out())
        with self.assertRaisesRegex(MaskError, "chunk_size"):
            run_mask([f], policy=policy, key=KEY,
                     output_dir=self._out(), chunk_size=0)

    def test_same_stem_rejected(self):
        sub = self.tmp / "sub"
        sub.mkdir()
        a = self._inputs("same.jsonl", [_rec("a.dat", 1, {"ACCT": "1"})])
        b = _write_jsonl(sub / "same.jsonl",
                         [_rec("b.dat", 1, {"ACCT": "2"})])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        with self.assertRaisesRegex(MaskInputError, "stem"):
            run_mask([a, b], policy=policy, key=KEY,
                     output_dir=self._out())

    def test_no_originals_or_key_in_outputs(self):
        secret = "1000002345"
        f = self._inputs("f.jsonl", [
            _rec("f.dat", 1, {"ACCT": secret, "NAME": "ZHANG WEI",
                              "AMT": "12345.67"}),
        ])
        policy = _policy([
            _rule(["ACCT"], "pseudonym", "account", group="g"),
            _rule(["NAME"], "synthesize", "name"),
            _rule(["AMT"], "shift", "amount", max_pct="5"),
        ])
        run_mask([f], policy=policy, key=KEY, output_dir=self._out())
        blob = "".join(p.read_text(encoding="utf-8")
                       for p in self._out().iterdir())
        for fragment in (secret, "ZHANG", "WEI", "12345.67",
                         "unit-test-key-material"):
            self.assertNotIn(fragment, blob)

    def test_empty_input_file(self):
        empty = self._inputs("empty.jsonl", [])
        f = self._inputs("f.jsonl", [_rec("f.dat", 1, {"ACCT": "1"})])
        policy = _policy([_rule(["ACCT"], "pseudonym", "account",
                                group="g")])
        summary = run_mask([empty, f], policy=policy, key=KEY,
                           output_dir=self._out())
        self.assertEqual(summary["records_in"], 1)
        self.assertEqual(
            self._read_jsonl(self._out() / "empty.masked.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
