"""Tests for offline Git bundle intake.

The hand-built fixtures use no ``git`` binary: object ids and blob bytes
are literal, so the reader is checked against an independent expectation
of the Git object format rather than against itself.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from copybook.gitkit import (BundleFormatError, DeltaError, ExportError,
                             GitHandoffError, HandoffQueryError,
                             ObjectFormatError, PackFormatError,
                             ResourceLimitError, TreeFormatError,
                             decode_path, encode_path, export_blob,
                             ingest_handoff, inspect_handoff,
                             read_bundle_layout)
from copybook.gitkit.bundle import extract_pack, parse_pack
from copybook.gitkit.pack import Limits, PackStore, Staging, apply_delta

from tests.gitkit import fixtures as F
from tests.gitkit import packutil as P


class GitKitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gitkit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_bundle(self, data: bytes, name: str = "b.bundle") -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def make_store(self, entries) -> PackStore:
        pack_bytes = P.build_pack(entries)
        bundle = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"],
            pack_bytes)
        bundle_path = self.write_bundle(bundle)
        layout = read_bundle_layout(bundle_path)
        pack_path = self.tmp / "p.pack"
        extract_pack(layout, pack_path)
        pack = parse_pack(pack_path, max_stream=1 << 20,
                          max_total=1 << 30)
        return PackStore(pack, Staging(self.tmp / "staging",
                                       max_bytes=1 << 30), Limits()).load()


class DeltaUnitTests(GitKitCase):
    def test_copy_and_insert(self):
        base = b"0123456789abcdef"
        delta = (P.encode_varint(len(base)) + P.encode_varint(16)
                 + bytes([0x80 | 0x01 | 0x10, 4, 4])   # copy [4, 8)
                 + bytes([4]) + b"WXYZ"                 # insert
                 + bytes([0x80 | 0x01 | 0x10, 0, 8]))   # copy [0, 8)
        self.assertEqual(apply_delta(base, delta),
                         b"4567" + b"WXYZ" + b"01234567")
        self.assertEqual(len(apply_delta(base, delta)), 16)

    def test_copy_size_zero_means_64k(self):
        base = b"x" * 0x10000
        delta = (P.encode_varint(0x10000) + P.encode_varint(0x10000)
                 + bytes([0x80 | 0x10 | 0x20, 0x00, 0x00]))
        self.assertEqual(apply_delta(base, delta), base)

    def test_source_size_mismatch(self):
        delta = (P.encode_varint(99) + P.encode_varint(1) + bytes([1]) + b"x")
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)

    def test_copy_past_base(self):
        delta = (P.encode_varint(3) + P.encode_varint(4)
                 + bytes([0x80 | 0x01 | 0x10, 2, 4]))
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)

    def test_insert_past_target(self):
        delta = P.encode_varint(3) + P.encode_varint(2) + bytes([4]) + b"abcd"
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)

    def test_insert_past_stream(self):
        delta = P.encode_varint(3) + P.encode_varint(4) + bytes([4]) + b"ab"
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)

    def test_target_size_mismatch(self):
        delta = P.encode_varint(3) + P.encode_varint(9) + bytes([2]) + b"ab"
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)

    def test_zero_opcode_is_reserved(self):
        delta = P.encode_varint(3) + P.encode_varint(1) + bytes([0])
        with self.assertRaises(DeltaError):
            apply_delta(b"abc", delta)


class PackStoreTests(GitKitCase):
    def test_ref_delta_before_base(self):
        base = b"base content here\n"
        target = base + b"tail\n"
        store = self.make_store([
            {"type": P.OBJ_REF_DELTA,
             "base": ("#ref", P.object_id(P.OBJ_BLOB, base)),
             "delta": P.make_delta(base, target)},
            {"type": P.OBJ_BLOB, "content": base},
        ])
        target_id = P.object_id(P.OBJ_BLOB, target)
        self.assertIn(target_id, store.infos)
        self.assertEqual(store.read(target_id), target)
        # the base is untouched by resolving the target
        self.assertEqual(store.read(P.object_id(P.OBJ_BLOB, base)), base)

    def test_mixed_chain_shares_one_base(self):
        base = b"shared base\n"
        mid = base + b"mid\n"                  # REF delta over base
        leaf = mid + b"leaf\n"                 # OFS delta over mid
        other = base + b"other\n"              # OFS delta over base
        entries = [
            {"type": P.OBJ_BLOB, "content": base},       # index 0
            {"type": P.OBJ_REF_DELTA,
             "base": ("#ref", P.object_id(P.OBJ_BLOB, base)),
             "delta": P.make_delta(base, mid)},          # index 1
            {"type": P.OBJ_OFS_DELTA, "base": ("#ofs", 1),
             "delta": P.make_delta(mid, leaf)},          # index 2
            {"type": P.OBJ_OFS_DELTA, "base": ("#ofs", 0),
             "delta": P.make_delta(base, other)},        # index 3
        ]
        store = self.make_store(entries)
        self.assertEqual(store.read(P.object_id(P.OBJ_BLOB, leaf)), leaf)
        self.assertEqual(store.read(P.object_id(P.OBJ_BLOB, other)), other)
        self.assertEqual(store.read(P.object_id(P.OBJ_BLOB, mid)), mid)
        self.assertGreaterEqual(store.max_depth_seen, 2)

    def test_duplicate_object_id_different_size(self):
        # Two non-delta entries can only collide on id if their content
        # collides; use two *identical* entries to show that a repeat is
        # folded into one fact and does not corrupt the store.
        content = b"same bytes\n"
        store = self.make_store([
            {"type": P.OBJ_BLOB, "content": content},
            {"type": P.OBJ_BLOB, "content": content},
        ])
        self.assertEqual(len(store.infos), 1)

    def test_missing_ref_base_is_diagnosed(self):
        missing = "0" * 39 + "1"
        with self.assertRaises(DeltaError) as ctx:
            self.make_store([
                {"type": P.OBJ_REF_DELTA, "base": ("#ref", missing),
                 "delta": P.make_delta(b"", b"x")},
            ])
        self.assertIn(missing, str(ctx.exception))

    def test_declared_size_mismatch(self):
        with self.assertRaises(PackFormatError):
            self.make_store([
                {"type": P.OBJ_BLOB, "content": b"abc",
                 "declared_size": 4},
            ])

    def test_unknown_pack_type(self):
        # Type 5 is not a valid pack object type.
        pack_bytes = bytearray(b"PACK\x00\x00\x00\x02\x00\x00\x00\x01")
        pack_bytes += bytes([0x50, 0x01]) + __import__("zlib").compress(b"x")
        import hashlib
        pack = bytes(pack_bytes) + hashlib.sha1(pack_bytes).digest()
        bundle = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"], pack)
        path = self.write_bundle(bundle)
        layout = read_bundle_layout(path)
        extract_pack(layout, self.tmp / "x.pack")
        with self.assertRaises(PackFormatError):
            parse_pack(self.tmp / "x.pack", max_stream=1 << 20,
                       max_total=1 << 30)

    def test_pack_object_count_lie(self):
        pack = P.build_pack([{"type": P.OBJ_BLOB, "content": b"x"}],
                            object_count_override=2)
        bundle = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"], pack)
        path = self.write_bundle(bundle)
        layout = read_bundle_layout(path)
        self.assertEqual(layout.object_count, 2)
        extract_pack(layout, self.tmp / "x.pack")
        with self.assertRaises(PackFormatError):
            parse_pack(self.tmp / "x.pack", max_stream=1 << 20,
                       max_total=1 << 30)

    def test_pack_trailing_byte_inside_frame(self):
        # A pack whose data has one unexplained byte before its trailer,
        # with a *correct* trailer for that longer data region.  The frame
        # parser must notice the entry count/offset mismatch rather than
        # accepting the pack.
        pack = P.build_pack([{"type": P.OBJ_BLOB, "content": b"x"}])
        import hashlib
        body = pack[:-20] + b"\x00"
        mangled = body + hashlib.sha1(body).digest()
        bundle = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"],
            mangled)
        path = self.write_bundle(bundle)
        layout = read_bundle_layout(path)
        extract_pack(layout, self.tmp / "x.pack")
        with self.assertRaises(PackFormatError):
            parse_pack(self.tmp / "x.pack", max_stream=1 << 20,
                       max_total=1 << 30)

    def test_single_object_limit(self):
        with self.assertRaises(GitHandoffError):
            pack_bytes = P.build_pack(
                [{"type": P.OBJ_BLOB, "content": b"x" * 64}])
            bundle = P.build_bundle(
                ["1111111111111111111111111111111111111111 refs/heads/x"],
                pack_bytes)
            path = self.write_bundle(bundle)
            layout = read_bundle_layout(path)
            extract_pack(layout, self.tmp / "x.pack")
            pack = parse_pack(self.tmp / "x.pack", max_stream=16,
                              max_total=1 << 30)
            PackStore(pack, Staging(self.tmp / "s2", max_bytes=1 << 30),
                      Limits(max_object_bytes=16,
                             max_total_bytes=1 << 30)).load()

    def test_delta_depth_limit(self):
        base = b"a\n"
        entries = []
        current = base
        for _ in range(4):
            nxt = current + b"a\n"
            entries.append({"type": P.OBJ_REF_DELTA,
                            "base": ("#ref", P.object_id(P.OBJ_BLOB,
                                                         current)),
                            "delta": P.make_delta(current, nxt)})
            current = nxt
        entries.append({"type": P.OBJ_BLOB, "content": base})
        pack_bytes = P.build_pack(entries)
        bundle = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"],
            pack_bytes)
        path = self.write_bundle(bundle)
        layout = read_bundle_layout(path)
        extract_pack(layout, self.tmp / "deep.pack")
        pack = parse_pack(self.tmp / "deep.pack", max_stream=1 << 20,
                          max_total=1 << 30)
        with self.assertRaises(ResourceLimitError):
            PackStore(pack, Staging(self.tmp / "s3", max_bytes=1 << 30),
                      Limits(max_delta_depth=2)).load()


class BundleFormatTests(GitKitCase):
    def _good_pack(self):
        return P.build_pack([{"type": P.OBJ_BLOB, "content": b"x"}])

    def test_header_signature(self):
        bad = b"# v3 git bundle\n\n" + self._good_pack()
        with self.assertRaises(BundleFormatError):
            read_bundle_layout(self.write_bundle(bad))

    def test_sha256_capability_rejected(self):
        body = b"# v2 git bundle\n@object-format=sha256\n" \
               b"1111111111111111111111111111111111111111 refs/heads/x\n\n"
        with self.assertRaises(BundleFormatError) as ctx:
            read_bundle_layout(self.write_bundle(body + b""))
        self.assertIn("capability", str(ctx.exception))

    def test_prerequisite_rejected(self):
        body = (b"# v2 git bundle\n"
                b"-1111111111111111111111111111111111111111\n"
                b"2222222222222222222222222222222222222222 refs/heads/x\n\n")
        with self.assertRaises(BundleFormatError) as ctx:
            read_bundle_layout(self.write_bundle(body))
        self.assertIn("prerequisite", str(ctx.exception))

    def test_64_hex_ref_rejected(self):
        body = (b"# v2 git bundle\n"
                + b"1" * 64 + b" refs/heads/x\n\n")
        with self.assertRaises(BundleFormatError) as ctx:
            read_bundle_layout(self.write_bundle(body))
        self.assertIn("SHA-256", str(ctx.exception))

    def test_extra_tail_byte_rejected(self):
        pack = self._good_pack()
        data = P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"], pack)
        with self.assertRaises(BundleFormatError) as ctx:
            read_bundle_layout(self.write_bundle(data + b"\x00"))
        self.assertIn("SHA-1", str(ctx.exception))

    def test_trailer_corruption(self):
        pack = self._good_pack()
        data = bytearray(P.build_bundle(
            ["1111111111111111111111111111111111111111 refs/heads/x"], pack))
        data[-1] ^= 0xFF
        with self.assertRaises(BundleFormatError):
            read_bundle_layout(self.write_bundle(bytes(data)))

    def test_header_only_bundle_rejected_by_ingest(self):
        path = self.write_bundle(
            b"# v2 git bundle\n"
            b"1111111111111111111111111111111111111111 refs/heads/x\n\n"
            + b"\x00" * 20)
        with self.assertRaises(BundleFormatError):
            ingest_handoff(path, output_dir=self.tmp / "out",
                           temp_dir=self.tmp)


class FixtureBundleTests(GitKitCase):
    def setUp(self):
        super().setUp()
        self.bundle = F.build_fixture_bundle(self.tmp)
        self.report = inspect_handoff(self.bundle, temp_dir=self.tmp)

    def test_commit_order_follows_parents_not_dates(self):
        order = self.report["commit_order"]
        self.assertLess(order.index(F.COMMIT1_ID), order.index(F.COMMIT2_ID))
        self.assertLess(order.index(F.COMMIT2_ID), order.index(F.COMMIT3_ID))
        self.assertEqual(order.index(F.COMMIT1_ID), 0)
        node = next(c for c in self.report["commits"]
                    if c["object_id"] == F.COMMIT2_ID)
        self.assertLess(node["committer"]["timestamp"], 1700000500)

    def test_merge_commit_diffed_against_each_parent(self):
        rows = self.report["diffs"][F.COMMIT3_ID]
        self.assertEqual([r["parent_index"] for r in rows], [0, 1])
        self.assertEqual([r["parent"] for r in rows],
                         [F.COMMIT2_ID, F.COMMIT1_ID])
        # vs parent 0 (tree_second): conf file -> directory, run.sh mode
        first = {(c["change"], c["path"]["display"]): c
                 for c in rows[0]["changes"]}
        self.assertIn(("deleted", "conf"), first)
        self.assertIn(("added", "conf/x"), first)
        self.assertTrue(first[("modified", "run.sh")]["mode_changed"])
        # vs parent 1 (base tree): alpha.txt content, run.sh mode
        second = {(c["change"], c["path"]["display"]): c
                  for c in rows[1]["changes"]}
        self.assertIn(("modified", "alpha.txt"), second)
        self.assertTrue(second[("modified", "alpha.txt")]["content_changed"])
        self.assertFalse(second[("modified", "alpha.txt")]["mode_changed"])
        # run.sh is 100755 on both sides here: no change at all
        self.assertNotIn("run.sh",
                         [c["path"]["display"] for c in rows[1]["changes"]])
        self.assertIn(("added", "new.txt"), second)
        self.assertNotIn(("deleted", "conf"), second)

    def test_mode_only_change_reported(self):
        rows = self.report["diffs"][F.COMMIT2_ID]
        change = next(c for c in rows[0]["changes"]
                      if c["path"]["display"] == "run.sh")
        self.assertTrue(change["mode_changed"])
        self.assertFalse(change["content_changed"])
        self.assertEqual(change["before"]["mode"], "100755")
        self.assertEqual(change["after"]["mode"], "100644")

    def test_content_change_and_same_blob_two_paths(self):
        listing = self.report["file_listings"][F.COMMIT1_ID]["files"]
        alpha = [f for f in listing
                 if f["path"]["display"] == "alpha.txt"][0]
        self.assertEqual(alpha["object_id"], F.BLOB_A_ID)
        self.assertEqual(alpha["size"], len(F.BLOB_A))
        # The same blob is referenced by two different tree entries
        # (alpha.txt and conf/x) and is one object with two file facts.
        ids = [f["object_id"] for f in listing]
        self.assertEqual(ids.count(F.BLOB_A_ID), 2)

    def test_newline_and_non_utf8_names(self):
        listing = self.report["file_listings"][F.COMMIT1_ID]["files"]
        by_b64 = {f["path"]["b64"]: f for f in listing}
        nl = by_b64[encode_path([b"line\nbreak"])]
        self.assertEqual(nl["object_id"], F.BLOB_NL_ID)
        raw = by_b64[encode_path([b"\xffraw"])]
        self.assertEqual(raw["object_id"], F.BLOB_RAW_ID)
        # no path is carried as plain newline-joined text
        for fact in listing:
            self.assertNotIn("\n", fact["path"]["b64"])
        self.assertEqual(decode_path(nl["path"]["b64"]), (b"line\nbreak",))

    def test_symlink_and_gitlink_are_distinct(self):
        listing = self.report["file_listings"][F.COMMIT1_ID]["files"]
        kinds = {f["path"]["display"]: f["kind"] for f in listing}
        self.assertEqual(kinds["link"], "symlink")
        self.assertEqual(kinds["vendor"], "gitlink")
        self.assertEqual(kinds["run.sh"], "executable")
        self.assertEqual(kinds["alpha.txt"], "file")
        gitlink = next(f for f in listing if f["kind"] == "gitlink")
        self.assertEqual(gitlink["object_id"], F.GITLINK_ID)
        self.assertIsNone(gitlink["size"])
        # the gitlink target is absent from the pack, which is legal
        self.assertNotIn(F.GITLINK_ID, self.report["commits"][0]["message"])

    def test_root_tree_is_not_a_file(self):
        listing = self.report["file_listings"][F.COMMIT1_ID]["files"]
        self.assertFalse(any(f["path"]["b64"] == "" for f in listing))
        self.assertFalse(any(f["kind"] == "tree" for f in listing))

    def test_files_are_not_followed_into_gitlink(self):
        out = self.tmp / "facts"
        ingest_handoff(self.bundle, output_dir=out, temp_dir=self.tmp)
        names = [p.name for p in (out / "commits").iterdir()]
        self.assertTrue(all("vendor" not in n for n in names))

    def test_duplicate_shared_refs(self):
        nodes = {c["object_id"]: c for c in self.report["commits"]}
        self.assertEqual(nodes[F.COMMIT1_ID]["refs"],
                         ["HEAD", "refs/heads/main", "refs/heads/side"])

    def test_export_and_refusals(self):
        out = self.tmp / "export.bin"
        manifest = export_blob(
            self.bundle, commit_id=F.COMMIT1_ID,
            path=encode_path([b"alpha.txt"]), destination=out,
            temp_dir=self.tmp)
        self.assertEqual(out.read_bytes(), F.BLOB_A)
        self.assertEqual(manifest["blob"]["object_id"], F.BLOB_A_ID)
        self.assertEqual(manifest["mode"], "100644")
        with self.assertRaises(ExportError):
            export_blob(self.bundle, commit_id=F.COMMIT1_ID,
                        path=encode_path([b"link"]),
                        destination=self.tmp / "link.bin", temp_dir=self.tmp)
        with self.assertRaises(ExportError):
            export_blob(self.bundle, commit_id=F.COMMIT1_ID,
                        path=encode_path([b"vendor"]),
                        destination=self.tmp / "vendor.bin",
                        temp_dir=self.tmp)
        with self.assertRaises(ExportError):
            export_blob(self.bundle, commit_id=F.COMMIT1_ID, path="",
                        destination=self.tmp / "root.bin",
                        temp_dir=self.tmp)
        # existing destination is never overwritten
        existing = self.tmp / "x.bin"
        existing.write_bytes(b"keep\n")
        with self.assertRaises(ExportError):
            export_blob(self.bundle, commit_id=F.COMMIT1_ID,
                        path=encode_path([b"alpha.txt"]),
                        destination=existing, temp_dir=self.tmp)
        self.assertEqual(existing.read_bytes(), b"keep\n")
        # unknown commit is refused
        with self.assertRaises(ExportError):
            export_blob(self.bundle, commit_id="f" * 40,
                        path=encode_path([b"alpha.txt"]),
                        destination=self.tmp / "y.bin", temp_dir=self.tmp)

    def test_output_dir_never_overwritten(self):
        out = self.tmp / "facts"
        ingest_handoff(self.bundle, output_dir=out, temp_dir=self.tmp)
        (out / "marker").write_text("keep")
        with self.assertRaises(HandoffQueryError):
            ingest_handoff(self.bundle, output_dir=out, temp_dir=self.tmp)
        self.assertEqual((out / "marker").read_text(), "keep")

    def test_staging_cleaned_up(self):
        stage = self.tmp / "stage"
        stage.mkdir()
        ingest_handoff(self.bundle, output_dir=self.tmp / "out2",
                       temp_dir=stage)
        self.assertEqual(list(stage.iterdir()), [])


class CorruptFixtureTests(GitKitCase):
    """Corruptions that must execute an internal check, not just the
    outer checksum: each copy re-seals the pack trailer so the damage is
    what the reader complains about."""

    def setUp(self):
        super().setUp()
        self.bundle = F.build_fixture_bundle(self.tmp)
        from copybook.gitkit import read_bundle_layout as rbl
        layout = rbl(self.bundle)
        self.original = self.bundle.read_bytes()
        self.pack_offset = layout.pack_offset

    def _resealed(self, mutate) -> Path:
        data = bytearray(self.original)
        pack = bytearray(data[self.pack_offset:])
        mutate(pack)
        import hashlib
        if pack[-20:] == hashlib.sha1(pack[:-20]).digest():
            pass
        pack[-20:] = hashlib.sha1(bytes(pack[:-20])).digest()
        data[self.pack_offset:] = pack
        return self.write_bundle(bytes(data), "corrupt.bundle")

    def test_truncated_delta_payload(self):
        """A REF delta whose copy range is corrupted (re-sealed trailer)."""
        pack_bytes, _ = F.build_fixture_pack()
        # Find the first delta entry (index 0 is the first REF_DELTA).
        import zlib
        from copybook.gitkit.bundle import parse_pack  # noqa: F401
        delta = P.make_delta(F.BLOB_A, F.BLOB_B)
        bad = bytearray(delta)
        # The first instruction after the two size varints is a copy; make
        # its offset/size point far past the base.
        pos = 0
        for _ in range(2):
            while bad[pos] & 0x80:
                pos += 1
            pos += 1
        bad[pos] = 0x80 | 0x01 | 0x10
        bad[pos + 1] = 0xFF
        bad[pos + 2] = 0x7F
        entries = [
            {"type": P.OBJ_REF_DELTA, "base": ("#ref", F.BLOB_A_ID),
             "delta": bytes(bad)},
            {"type": P.OBJ_BLOB, "content": F.BLOB_A},
        ]
        pack = P.build_pack(entries)
        data = P.build_bundle(
            [f"{F.COMMIT1_ID} refs/heads/x"], pack)
        path = self.write_bundle(data, "bad-delta.bundle")
        with self.assertRaises(DeltaError):
            from copybook.gitkit import read_bundle_layout as rbl
            layout = rbl(path)
            extract_pack(layout, self.tmp / "bad.pack")
            pack_layout = parse_pack(self.tmp / "bad.pack",
                                     max_stream=1 << 20,
                                     max_total=1 << 30)
            PackStore(pack_layout,
                      Staging(self.tmp / "stage2", max_bytes=1 << 30),
                      Limits()).load()

    def test_missing_base_object(self):
        delta = P.make_delta(F.BLOB_A, F.BLOB_B)
        pack = P.build_pack([
            {"type": P.OBJ_REF_DELTA,
             "base": ("#ref", "e" * 40), "delta": delta}])
        data = P.build_bundle([f"{F.COMMIT1_ID} refs/heads/x"], pack)
        path = self.write_bundle(data, "missing-base.bundle")
        layout = read_bundle_layout(path)
        extract_pack(layout, self.tmp / "mb.pack")
        pack_layout = parse_pack(self.tmp / "mb.pack", max_stream=1 << 20,
                                 max_total=1 << 30)
        with self.assertRaises(DeltaError) as ctx:
            PackStore(pack_layout,
                      Staging(self.tmp / "stage3", max_bytes=1 << 30),
                      Limits()).load()
        self.assertIn("e" * 40, str(ctx.exception))

    def test_tree_points_at_missing_object(self):
        missing = "d" * 40
        bad_tree = F.tree([("100644", b"ghost", missing)])
        bad_tree_id = F.git_oid("tree", bad_tree)
        bad_commit = F.commit(bad_tree_id, [], "broken tree\n")
        bad_commit_id = F.git_oid("commit", bad_commit)
        pack = P.build_pack([
            {"type": P.OBJ_TREE, "content": bad_tree},
            {"type": P.OBJ_COMMIT, "content": bad_commit},
        ])
        data = P.build_bundle([f"{bad_commit_id} refs/heads/x"], pack)
        path = self.write_bundle(data, "bad-tree.bundle")
        with self.assertRaises(TreeFormatError):
            inspect_handoff(path, temp_dir=self.tmp)

    def test_tree_type_mismatch(self):
        # A tree entry names a mode of "tree" but points at a blob.
        blob_id = F.git_oid("blob", F.BLOB_E)
        bad_tree = F.tree([("40000", b"sub", blob_id)])
        bad_tree_id = F.git_oid("tree", bad_tree)
        bad_commit = F.commit(bad_tree_id, [], "type mismatch\n")
        bad_commit_id = F.git_oid("commit", bad_commit)
        pack = P.build_pack([
            {"type": P.OBJ_BLOB, "content": F.BLOB_E},
            {"type": P.OBJ_TREE, "content": bad_tree},
            {"type": P.OBJ_COMMIT, "content": bad_commit},
        ])
        data = P.build_bundle([f"{bad_commit_id} refs/heads/x"], pack)
        path = self.write_bundle(data, "mismatch.bundle")
        with self.assertRaises(TreeFormatError):
            inspect_handoff(path, temp_dir=self.tmp)

    def test_duplicate_tree_name(self):
        tree = F.tree([("100644", b"same", F.BLOB_A_ID),
                       ("100644", b"same", F.BLOB_E_ID)])
        tree_id = F.git_oid("tree", tree)
        commit = F.commit(tree_id, [], "dup\n")
        commit_id = F.git_oid("commit", commit)
        pack = P.build_pack([
            {"type": P.OBJ_BLOB, "content": F.BLOB_A},
            {"type": P.OBJ_BLOB, "content": F.BLOB_E},
            {"type": P.OBJ_TREE, "content": tree},
            {"type": P.OBJ_COMMIT, "content": commit},
        ])
        data = P.build_bundle([f"{commit_id} refs/heads/x"], pack)
        with self.assertRaises(TreeFormatError):
            inspect_handoff(self.write_bundle(data), temp_dir=self.tmp)

    def test_dot_component_rejected(self):
        for name in (b".", b"..", b"a/b", b""):
            tree = F.tree([("100644", name, F.BLOB_A_ID)])
            tree_id = F.git_oid("tree", tree)
            commit = F.commit(tree_id, [], "bad name\n")
            commit_id = F.git_oid("commit", commit)
            pack = P.build_pack([
                {"type": P.OBJ_BLOB, "content": F.BLOB_A},
                {"type": P.OBJ_TREE, "content": tree},
                {"type": P.OBJ_COMMIT, "content": commit},
            ])
            data = P.build_bundle([f"{commit_id} refs/heads/x"], pack)
            with self.assertRaises(TreeFormatError, msg=repr(name)):
                inspect_handoff(self.write_bundle(data, "name.bundle"),
                                temp_dir=self.tmp)

    def test_invalid_mode_rejected(self):
        tree = F.tree([("100600", b"odd", F.BLOB_A_ID)])
        # 100600 is not a legal git mode
        tree_id = F.git_oid("tree", tree)
        commit = F.commit(tree_id, [], "mode\n")
        commit_id = F.git_oid("commit", commit)
        pack = P.build_pack([
            {"type": P.OBJ_BLOB, "content": F.BLOB_A},
            {"type": P.OBJ_TREE, "content": tree},
            {"type": P.OBJ_COMMIT, "content": commit},
        ])
        data = P.build_bundle([f"{commit_id} refs/heads/x"], pack)
        with self.assertRaises(TreeFormatError):
            inspect_handoff(self.write_bundle(data), temp_dir=self.tmp)

    def test_ref_points_at_tree(self):
        tree = F.tree([])
        tree_id = F.git_oid("tree", tree)
        pack = P.build_pack([{"type": P.OBJ_TREE, "content": tree}])
        data = P.build_bundle([f"{tree_id} refs/heads/x"], pack)
        with self.assertRaises(GitHandoffError):
            inspect_handoff(self.write_bundle(data), temp_dir=self.tmp)

    def test_parent_cycle_rejected(self):
        # Two commits that list each other as parents.  Ids cannot be
        # mutually consistent (SHA-1), so build a cycle in the graph layer
        # by pointing a parent at a commit whose own parent points back:
        # a self-parent commit is the simplest real cycle.
        tree_id = F.TREE_BASE_ID
        placeholder = "0" * 40
        content = F.commit(tree_id, [placeholder], "self parent\n")
        real_id = F.git_oid("commit", content)
        # Rebuild with its own id as parent.
        content = F.commit(tree_id, [real_id], "self parent\n")
        real_id2 = F.git_oid("commit", content)
        content = F.commit(tree_id, [real_id2], "self parent\n")
        real_id3 = F.git_oid("commit", content)
        pack = P.build_pack([
            {"type": P.OBJ_TREE, "content": F.TREE_BASE},
            {"type": P.OBJ_TREE, "content": F.TREE_CONF},
            {"type": P.OBJ_BLOB, "content": F.BLOB_A},
            {"type": P.OBJ_BLOB, "content": F.BLOB_E},
            {"type": P.OBJ_BLOB, "content": F.BLOB_LINK},
            {"type": P.OBJ_BLOB, "content": F.BLOB_NL},
            {"type": P.OBJ_BLOB, "content": F.BLOB_RAW},
            {"type": P.OBJ_COMMIT, "content": content},
        ])
        data = P.build_bundle([f"{real_id3} refs/heads/x"], pack)
        with self.assertRaises(GitHandoffError):
            inspect_handoff(self.write_bundle(data), temp_dir=self.tmp)


class GraphCycleUnitTests(GitKitCase):
    def test_cycle_detector_and_topo_order(self):
        from copybook.gitkit.errors import CommitGraphError
        from copybook.gitkit.graph import CommitNode, _check_acyclic, _topo_order
        from copybook.gitkit.objects import CommitHeaders

        def node(oid, parents):
            headers = CommitHeaders(
                tree="f" * 40, parents=list(parents),
                author_raw="A <a@b> 1 +0000",
                committer_raw="A <a@b> 1 +0000",
                author={}, committer={}, extra_headers=[],
                message="", message_bytes=b"")
            return CommitNode(oid, headers)

        a = "aa" * 20
        b = "bb" * 20
        nodes = {a: node(a, [b]), b: node(b, [a])}
        with self.assertRaises(CommitGraphError):
            _check_acyclic(nodes)
        with self.assertRaises(CommitGraphError):
            _topo_order(nodes)
        # a child listed "before" its parent by id still sorts after it
        child = "00" * 20
        parent = "ff" * 20
        nodes = {child: node(child, [parent]), parent: node(parent, [])}
        self.assertEqual(_topo_order(nodes), [parent, child])

    def test_report_states_no_extraction(self):
        bundle = F.build_fixture_bundle(self.tmp)
        report = inspect_handoff(bundle, temp_dir=self.tmp)
        self.assertFalse(report["extraction"]["performed"])


class ManifestAndQueryTests(GitKitCase):
    def test_unknown_commit_query_fails(self):
        bundle = F.build_fixture_bundle(self.tmp)
        with self.assertRaises(ExportError):
            export_blob(bundle, commit_id="a" * 40,
                        path=encode_path([b"alpha.txt"]),
                        destination=self.tmp / "o", temp_dir=self.tmp)

    def test_bad_path_encoding(self):
        for bad in ("!!!!", "YWxwaGEudHh0//", "YQ==/", "YQ"):  # padding/empty
            with self.assertRaises(HandoffQueryError, msg=bad):
                decode_path(bad)
        self.assertEqual(decode_path("YQ=="), (b"a",))

    def test_cli_exit_codes(self):
        bundle = F.build_fixture_bundle(self.tmp)
        from copybook.__main__ import main
        out = self.tmp / "cli-facts"
        rc = main(["git-ingest", str(bundle), "--out-dir", str(out),
                   "--temp-dir", str(self.tmp)])
        self.assertEqual(rc, 0)
        rc = main(["git-ingest", str(bundle), "--out-dir", str(out),
                   "--temp-dir", str(self.tmp)])
        self.assertEqual(rc, 2)  # refuses to overwrite
        dest = self.tmp / "cli-export.bin"
        rc = main(["git-export", str(bundle), F.COMMIT1_ID,
                   "--path", encode_path([b"alpha.txt"]),
                   "--out", str(dest), "--temp-dir", str(self.tmp)])
        self.assertEqual(rc, 0)
        self.assertEqual(dest.read_bytes(), F.BLOB_A)
        rc = main(["git-export", str(bundle), F.COMMIT1_ID,
                   "--path", encode_path([b"alpha.txt"]),
                   "--out", str(dest), "--temp-dir", str(self.tmp)])
        self.assertEqual(rc, 2)  # existing destination
        rc = main(["git-query", str(bundle), "--temp-dir", str(self.tmp),
                   "--log"])
        self.assertEqual(rc, 0)
        rc = main(["git-query", str(bundle), "--temp-dir", str(self.tmp),
                   "--commit", F.COMMIT1_ID, "--files"])
        self.assertEqual(rc, 0)
        rc = main(["git-query", str(bundle), "--temp-dir", str(self.tmp),
                   "--files", "--commit", "b" * 40])
        self.assertEqual(rc, 2)

    def test_corrupt_bundle_leaves_no_output(self):
        bundle = F.build_fixture_bundle(self.tmp)
        data = bytearray(bundle.read_bytes())
        data[-1] ^= 0xFF
        bad = self.tmp / "bad.bundle"
        bad.write_bytes(bytes(data))
        out = self.tmp / "bad-out"
        with self.assertRaises(GitHandoffError):
            ingest_handoff(bad, output_dir=out, temp_dir=self.tmp)
        self.assertFalse(out.exists())

    def test_limits_are_hard_refusals(self):
        bundle = F.build_fixture_bundle(self.tmp)
        out = self.tmp / "limited"
        with self.assertRaises(GitHandoffError) as ctx:
            ingest_handoff(bundle, output_dir=out, temp_dir=self.tmp,
                           max_object_bytes=8)
        self.assertIn("limit", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_delta_depth_limit_refusal(self):
        bundle = F.build_fixture_bundle(self.tmp)
        out = self.tmp / "limited-depth"
        with self.assertRaises(ResourceLimitError):
            ingest_handoff(bundle, output_dir=out, temp_dir=self.tmp,
                           max_delta_depth=1)
        self.assertFalse(out.exists())

    def test_export_manifest_written_next_to_file(self):
        bundle = F.build_fixture_bundle(self.tmp)
        dest = self.tmp / "blob.bin"
        manifest = export_blob(bundle, commit_id=F.COMMIT1_ID,
                               path=encode_path([b"alpha.txt"]),
                               destination=dest, temp_dir=self.tmp)
        sidecar = self.tmp / "blob.bin.export_manifest.json"
        self.assertTrue(sidecar.is_file())
        stored = json.loads(sidecar.read_text())
        self.assertEqual(stored["blob"]["object_id"], F.BLOB_A_ID)
        self.assertEqual(stored["bundle"]["sha1"],
                         read_bundle_layout(bundle).digest)
        self.assertEqual(stored["path"]["b64"],
                         encode_path([b"alpha.txt"]))
        self.assertEqual(manifest["tree_chain"][0]["root_tree"],
                         F.TREE_BASE_ID)

    def test_commit_raw_headers_and_message_preserved(self):
        # A commit with a multi-line (continuation) header and an unknown
        # header: both must survive verbatim.
        pgp = "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----"
        content = P.commit_bytes(
            F.TREE_BASE_ID, [],
            "A <a@example.invalid> 1700000000 +0000",
            "C <c@example.invalid> 1700000001 +0100",
            "subject\n\nbody\n",
            extra_headers=[("gpgsig", pgp.replace("\n", "\n ")),
                           ("x-custom", "value")])
        commit_id = F.git_oid("commit", content)
        pack = P.build_pack([
            {"type": P.OBJ_TREE, "content": F.TREE_BASE},
            {"type": P.OBJ_TREE, "content": F.TREE_CONF},
            {"type": P.OBJ_BLOB, "content": F.BLOB_A},
            {"type": P.OBJ_BLOB, "content": F.BLOB_E},
            {"type": P.OBJ_BLOB, "content": F.BLOB_LINK},
            {"type": P.OBJ_BLOB, "content": F.BLOB_NL},
            {"type": P.OBJ_BLOB, "content": F.BLOB_RAW},
            {"type": P.OBJ_COMMIT, "content": content},
        ])
        bundle = self.write_bundle(P.build_bundle(
            [f"{commit_id} refs/heads/x"], pack))
        report = inspect_handoff(bundle, temp_dir=self.tmp)
        node = report["commits"][0]
        import base64
        gpgsig = [h for h in node["raw_headers"] if h["name"] == "gpgsig"]
        self.assertEqual(len(gpgsig), 1)
        self.assertEqual(base64.b64decode(gpgsig[0]["value_b64"]).decode(),
                         pgp)
        self.assertIn({"name": "x-custom", "raw_value": "value",
                       "values": ["value"]}, node["extra_headers"])
        self.assertEqual(node["message"], "subject\n\nbody\n")
        self.assertEqual(base64.b64decode(node["message_b64"]),
                         b"subject\n\nbody\n")
        self.assertEqual(node["committer"]["tz_offset_minutes"], 60)

    def test_reports_written(self):
        bundle = F.build_fixture_bundle(self.tmp)
        out = self.tmp / "out"
        summary = ingest_handoff(bundle, output_dir=out, temp_dir=self.tmp)
        self.assertEqual(summary["commit_count"], 3)
        report = json.loads((out / "handoff_report.json").read_text())
        self.assertEqual(report["format"], "copybook-git-handoff/v1")
        self.assertEqual(report["counts"]["delta_object_count"], 3)
        self.assertTrue((out / "commits.jsonl").is_file())
        files = sorted((out / "commits").glob("*.files.jsonl"))
        self.assertEqual(len(files), 3)


if __name__ == "__main__":
    unittest.main()
