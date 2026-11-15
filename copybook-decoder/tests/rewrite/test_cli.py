"""Terminal command for the rewrite feature: argument wiring and codes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from copybook.__main__ import main
from tests.rewrite.fixtures import build_demo_file, sha256_hex, write_layouts
from tests.rewrite.test_engine import (DemoFixture, fill_record_digests,
                                       make_order, record_entry)


class RewriteCliTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.fx = DemoFixture(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _argv(self, order_path, out_dir):
        return [
            "rewrite", str(order_path), str(self.fx.source_path),
            "--out-dir", str(out_dir),
            "--temp-dir", str(self.fx.staging),
            "--copybook-base",
            str(self.fx.copybooks["ORDA.cpy"].parent)]

    def test_cli_success_exit_zero_and_delivery(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", [{
                "path": ["FLAG"], "expected_hex": rec[43:44].hex(),
                "value": "9"}], spans=self.fx.spans)])
        fill_record_digests(self.fx.source, order)
        order_path = self.fx.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        out_dir = self.fx.tmp / "cli-delivery"
        code = main(self._argv(order_path, out_dir))
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / "original.rewritten.bin").exists())
        evidence = json.loads(
            (out_dir / "rewrite_evidence.json").read_text("utf-8"))
        self.assertEqual(evidence["format"], "copybook-rewrite/v1")
        self.assertEqual(evidence["counts"]["changed_bytes"], 1)

    def test_cli_conflict_returns_nonzero_and_no_delivery(self):
        a0, a1 = self.fx.spans["A"]
        rec = self.fx.source[a0:a1]
        order = make_order(
            source=self.fx.source, spans=self.fx.spans,
            records=[record_entry("A", [
                {"path": ["ITEM-GROUP", 0, "ITEM-AMOUNT"],
                 "expected_hex": rec[24:28].hex(), "value": "405.04"},
                {"path": ["ITEM-GROUP", 0, "ITEM-NOTE"],
                 "expected_hex": rec[24:28].hex(), "value": "ZZZZ"}],
                spans=self.fx.spans)])
        fill_record_digests(self.fx.source, order)
        order_path = self.fx.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        out_dir = self.fx.tmp / "cli-conflict"
        code = main(self._argv(order_path, out_dir))
        self.assertNotEqual(code, 0)
        self.assertFalse(out_dir.exists())

    def test_cli_existing_output_dir_refused(self):
        order = make_order(source=self.fx.source, spans=self.fx.spans,
                           records=[])
        order_path = self.fx.tmp / "order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        out_dir = self.fx.tmp / "exists"
        out_dir.mkdir()
        code = main(self._argv(order_path, out_dir))
        self.assertNotEqual(code, 0)

    def test_original_entry_points_still_present(self):
        # the pre-existing decode subcommand and its parser remain wired
        with self.assertRaises(SystemExit) as caught:
            main(["decode", "--help"])
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
