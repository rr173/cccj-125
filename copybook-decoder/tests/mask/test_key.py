"""Mapping-key loading and fingerprint checks."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copybook.mask import MaskKey, MaskKeyError

GOOD_KEY = "unit-test-key-material-0123456789"


class TestMaskKey(unittest.TestCase):

    def test_from_env(self):
        with mock.patch.dict(os.environ, {"MASK_TEST_KEY": GOOD_KEY}):
            key = MaskKey.from_env("MASK_TEST_KEY")
        self.assertEqual(key.material, GOOD_KEY.encode())
        self.assertTrue(key.fingerprint.startswith("sha256:"))
        self.assertEqual(len(key.fingerprint), len("sha256:") + 16)

    def test_from_env_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MaskKeyError, "not set"):
                MaskKey.from_env("DEFINITELY_UNSET_MASK_KEY")

    def test_from_env_too_short(self):
        with mock.patch.dict(os.environ, {"MASK_TEST_KEY": "short"}):
            with self.assertRaisesRegex(MaskKeyError, "at least"):
                MaskKey.from_env("MASK_TEST_KEY")

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text(GOOD_KEY + "\n", encoding="utf-8")
            key = MaskKey.from_file(path)
        self.assertEqual(key.material, GOOD_KEY.encode())

    def test_from_file_missing(self):
        with self.assertRaisesRegex(MaskKeyError, "cannot read"):
            MaskKey.from_file("/nonexistent/mask-key.txt")

    def test_from_file_too_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text("tiny", encoding="utf-8")
            with self.assertRaisesRegex(MaskKeyError, "at least"):
                MaskKey.from_file(path)

    def test_fingerprint_check(self):
        with mock.patch.dict(os.environ, {"MASK_TEST_KEY": GOOD_KEY}):
            key = MaskKey.from_env("MASK_TEST_KEY")
        key.check_fingerprint(key.fingerprint)          # matches: no raise
        key.check_fingerprint(None)                     # absent: no raise
        with self.assertRaisesRegex(MaskKeyError, "does not match"):
            key.check_fingerprint("sha256:0000000000000000")
        with self.assertRaisesRegex(MaskKeyError, "malformed"):
            key.check_fingerprint("sha256:xyz")

    def test_distinct_keys_distinct_fingerprints(self):
        k1 = MaskKey._from_text(GOOD_KEY, "a")
        k2 = MaskKey._from_text(GOOD_KEY + "x", "b")
        self.assertNotEqual(k1.fingerprint, k2.fingerprint)


if __name__ == "__main__":
    unittest.main()
