"""Manifest: version selection by sender + business date, no guessing."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook import Manifest, ManifestError


def write_manifest(directory: Path, layouts: list[dict]) -> Path:
    path = directory / "manifest.json"
    path.write_text(json.dumps({"layouts": layouts}), encoding="utf-8")
    return path


def entry(sender="BANKA", copybook="ACCT", version="1",
          effective_from="2024-01-01", effective_to=None, **extra):
    e = {"sender": sender, "copybook": copybook, "version": version,
         "effective_from": effective_from, "file": f"{copybook}-{version}.cpy"}
    if effective_to is not None:
        e["effective_to"] = effective_to
    e.update(extra)
    return e


class ManifestTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def load(self, layouts) -> Manifest:
        return Manifest.load(write_manifest(self.dir, layouts))


class TestVersionSwitching(ManifestTestCase):
    def test_date_selects_version(self):
        manifest = self.load([
            entry(version="1", effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01"),
        ])
        self.assertEqual(
            manifest.resolve("BANKA", "2024-03-15").version, "1")
        self.assertEqual(
            manifest.resolve("BANKA", "2024-09-01").version, "2")
        self.assertEqual(
            manifest.resolve("BANKA", "2025-06-30").version, "2")  # open end

    def test_boundary_dates_are_inclusive(self):
        manifest = self.load([
            entry(version="1", effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01",
                  effective_to="2024-12-31"),
        ])
        self.assertEqual(manifest.resolve("BANKA", "2024-08-31").version, "1")
        self.assertEqual(manifest.resolve("BANKA", "2024-12-31").version, "2")

    def test_senders_are_independent(self):
        manifest = self.load([
            entry(sender="BANKA", version="1"),
            entry(sender="BANKB", version="7"),
        ])
        self.assertEqual(manifest.resolve("BANKA", "2024-05-01").version, "1")
        self.assertEqual(manifest.resolve("BANKB", "2024-05-01").version, "7")


class TestNoGuessing(ManifestTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.load([
            entry(version="1", effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01"),
        ])

    def test_unknown_sender_rejected(self):
        with self.assertRaises(ManifestError):
            self.manifest.resolve("UNKNOWN", "2024-05-01")

    def test_date_outside_all_ranges_rejected(self):
        with self.assertRaises(ManifestError):
            self.manifest.resolve("BANKA", "2020-01-01")

    def test_missing_sender_rejected(self):
        with self.assertRaises(ManifestError):
            self.manifest.resolve(None, "2024-05-01")
        with self.assertRaises(ManifestError):
            self.manifest.resolve("", "2024-05-01")

    def test_missing_date_rejected(self):
        with self.assertRaises(ManifestError):
            self.manifest.resolve("BANKA", None)

    def test_bad_date_rejected(self):
        with self.assertRaises(ManifestError):
            self.manifest.resolve("BANKA", "15-03-2024")

    def test_ambiguous_copybooks_rejected(self):
        manifest = self.load([
            entry(copybook="ACCT", version="1"),
            entry(copybook="CARD", version="3"),
        ])
        with self.assertRaises(ManifestError) as ctx:
            manifest.resolve("BANKA", "2024-05-01")
        self.assertIn("conflicting", str(ctx.exception))
        # ... but an explicit copybook name disambiguates
        self.assertEqual(
            manifest.resolve("BANKA", "2024-05-01", copybook="CARD").version,
            "3")


class TestLoadValidation(ManifestTestCase):
    def test_overlapping_ranges_rejected_at_load(self):
        with self.assertRaises(ManifestError) as ctx:
            self.load([
                entry(version="1", effective_to="2024-09-15"),
                entry(version="2", effective_from="2024-09-01"),
            ])
        self.assertIn("conflicting", str(ctx.exception))

    def test_adjacent_ranges_are_fine(self):
        manifest = self.load([
            entry(version="1", effective_to="2024-08-31"),
            entry(version="2", effective_from="2024-09-01"),
        ])
        self.assertEqual(len(manifest.entries), 2)

    def test_open_ended_overlap_rejected(self):
        with self.assertRaises(ManifestError):
            self.load([
                entry(version="1"),  # open-ended
                entry(version="2", effective_from="2024-09-01"),
            ])

    def test_missing_keys_rejected(self):
        with self.assertRaises(ManifestError):
            self.load([{"sender": "BANKA"}])

    def test_bad_dates_rejected(self):
        with self.assertRaises(ManifestError):
            self.load([entry(effective_from="31.12.2024")])
        with self.assertRaises(ManifestError):
            self.load([entry(effective_from="2024-12-31",
                             effective_to="2024-01-01")])

    def test_unknown_codec_rejected(self):
        with self.assertRaises(ManifestError):
            self.load([entry(codec="ebcdic-299")])

    def test_empty_manifest_rejected(self):
        with self.assertRaises(ManifestError):
            self.load([])


if __name__ == "__main__":
    unittest.main()
