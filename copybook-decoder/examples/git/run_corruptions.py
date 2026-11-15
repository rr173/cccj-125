#!/usr/bin/env python3
"""Corrupt-copy checks for the offline handoff reader.

Each case copies a *valid* bundle, damages one internal structure and
re-seals the outer checksum that would otherwise mask the damage, so the
reader really executes the inner check.  Expectations are literal: the
error class and a fragment of the diagnostic.

Usage (from the project root):
    python3 examples/git/run_corruptions.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.gitkit import (BundleFormatError, DeltaError, GitHandoffError,
                             PackFormatError, TreeFormatError,
                             inspect_handoff, parse_pack,
                             read_bundle_layout)
from copybook.gitkit.bundle import extract_pack
from copybook.gitkit.pack import Limits, PackStore, Staging

from tests.gitkit import fixtures as F
from tests.gitkit import packutil as P

REQUIRED_BYTES = 20


def bundle_of(pack: bytes, ref: str = None) -> bytes:
    return P.build_bundle([f"{ref or F.COMMIT1_ID} refs/heads/x"], pack)


def one_fixture_pack():
    pack, _ = F.build_fixture_pack()
    return pack


CASES = []


def case(name, expect, fragment):
    def wrap(fn):
        CASES.append((name, expect, fragment, fn))
        return fn
    return wrap


@case("delta copy range past base", DeltaError, "runs past")
def _(tmp):
    delta = bytearray(P.make_delta(F.BLOB_A, F.BLOB_B))
    pos = 0
    for _i in range(2):
        while delta[pos] & 0x80:
            pos += 1
        pos += 1
    delta[pos] = 0x80 | 0x01 | 0x10       # copy: 1 offset byte, 2 size bytes
    delta[pos + 1] = 0x7F                 # offset 127
    delta[pos + 2] = 0x7F
    delta[pos + 3] = 0x7F
    pack = P.build_pack([
        {"type": P.OBJ_REF_DELTA, "base": ("#ref", F.BLOB_A_ID),
         "delta": bytes(delta)},
        {"type": P.OBJ_BLOB, "content": F.BLOB_A},
    ])
    return _try_store(tmp, bundle_of(pack))


@case("missing REF_DELTA base", DeltaError, "missing from the bundle")
def _(tmp):
    pack = P.build_pack([
        {"type": P.OBJ_REF_DELTA, "base": ("#ref", "e" * 40),
         "delta": P.make_delta(F.BLOB_A, F.BLOB_B)},
        {"type": P.OBJ_BLOB, "content": F.BLOB_A},
    ])
    return _try_store(tmp, bundle_of(pack))


@case("declared object size too small", PackFormatError, "inflates")
def _(tmp):
    pack = P.build_pack([
        {"type": P.OBJ_BLOB, "content": F.BLOB_A,
         "declared_size": len(F.BLOB_A) - 2},
    ])
    return _try_parse(tmp, bundle_of(pack))


@case("declared object size too large", PackFormatError, "inflates")
def _(tmp):
    pack = P.build_pack([
        {"type": P.OBJ_BLOB, "content": F.BLOB_A,
         "declared_size": len(F.BLOB_A) + 2},
    ])
    return _try_parse(tmp, bundle_of(pack))


@case("tree points at a missing blob", TreeFormatError, "not present")
def _(tmp):
    tree = F.tree([("100644", b"ghost", "d" * 40)])
    tid = F.git_oid("tree", tree)
    commit = F.commit(tid, [], "ghost\n")
    cid = F.git_oid("commit", commit)
    pack = P.build_pack([
        {"type": P.OBJ_TREE, "content": tree},
        {"type": P.OBJ_COMMIT, "content": commit},
    ])
    return _try_inspect(tmp, bundle_of(pack, cid))


@case("tree entry mode does not match its object", TreeFormatError,
      "but points at")
def _(tmp):
    blob_id = F.git_oid("blob", F.BLOB_E)
    tree = F.tree([("40000", b"sub", blob_id)])
    tid = F.git_oid("tree", tree)
    commit = F.commit(tid, [], "mismatch\n")
    cid = F.git_oid("commit", commit)
    pack = P.build_pack([
        {"type": P.OBJ_BLOB, "content": F.BLOB_E},
        {"type": P.OBJ_TREE, "content": tree},
        {"type": P.OBJ_COMMIT, "content": commit},
    ])
    return _try_inspect(tmp, bundle_of(pack, cid))


@case("commit parent is a blob", GitHandoffError, "expected commit")
def _(tmp):
    blob_id = F.git_oid("blob", F.BLOB_E)
    commit = F.commit(F.TREE_BASE_ID, [blob_id], "bad parent\n")
    cid = F.git_oid("commit", commit)
    pack = P.build_pack([
        {"type": P.OBJ_BLOB, "content": F.BLOB_E},
        {"type": P.OBJ_TREE, "content": F.TREE_BASE},
        {"type": P.OBJ_TREE, "content": F.TREE_CONF},
        {"type": P.OBJ_BLOB, "content": F.BLOB_A},
        {"type": P.OBJ_BLOB, "content": F.BLOB_LINK},
        {"type": P.OBJ_BLOB, "content": F.BLOB_NL},
        {"type": P.OBJ_BLOB, "content": F.BLOB_RAW},
        {"type": P.OBJ_COMMIT, "content": commit},
    ])
    return _try_inspect(tmp, bundle_of(pack, cid))


@case("object-count field lies (extra byte before trailer)",
      PackFormatError, "")
def _(tmp):
    # Insert one unexplained byte and re-seal the outer pack checksum.
    pack = one_fixture_pack()
    body = pack[:-20] + b"\x00"
    return _try_parse(tmp, bundle_of(body +
                                     hashlib.sha1(body).digest()))


@case("bundle trailer corrupted", BundleFormatError, "SHA-1")
def _(tmp):
    data = bytearray(bundle_of(one_fixture_pack()))
    data[-1] ^= 0xFF
    path = tmp / "c.bundle"
    path.write_bytes(bytes(data))
    read_bundle_layout(path)
    return None


@case("extra byte after bundle trailer", BundleFormatError, "SHA-1")
def _(tmp):
    data = bundle_of(one_fixture_pack()) + b"\x00"
    path = tmp / "c.bundle"
    path.write_bytes(data)
    read_bundle_layout(path)
    return None


@case("truncated/corrupt zlib stream", PackFormatError, "")
def _(tmp):
    pack = bytearray(one_fixture_pack())
    # Flip bytes deep inside the first compressed stream (after the entry
    # header and base pointer) and re-seal the pack checksum; the stream
    # can no longer inflate to its declared content.
    for i in range(90, 94):
        pack[i] ^= 0xFF
    body = bytes(pack[:-20])
    pack = body + hashlib.sha1(body).digest()
    path = _write(tmp, bundle_of(pack))
    layout = read_bundle_layout(path)
    extract_pack(layout, tmp / "t.pack")
    parse_pack(tmp / "t.pack", max_stream=1 << 20, max_total=1 << 30)
    return None


def _write(tmp: Path, data: bytes) -> Path:
    path = tmp / "case.bundle"
    path.write_bytes(data)
    return path


def _try_store(tmp, bundle):
    path = _write(tmp, bundle)
    layout = read_bundle_layout(path)
    extract_pack(layout, tmp / "p.pack")
    pack = parse_pack(tmp / "p.pack", max_stream=1 << 20,
                      max_total=1 << 30)
    PackStore(pack, Staging(tmp / "stg", max_bytes=1 << 30),
              Limits()).load()
    return None


def _try_parse(tmp, bundle):
    path = _write(tmp, bundle)
    layout = read_bundle_layout(path)
    extract_pack(layout, tmp / "p.pack")
    parse_pack(tmp / "p.pack", max_stream=1 << 20, max_total=1 << 30)
    return None


def _try_inspect(tmp, bundle):
    inspect_handoff(_write(tmp, bundle), temp_dir=tmp)
    return None


def main() -> int:
    failures = 0
    for name, expect, fragment, fn in CASES:
        tmp = Path(tempfile.mkdtemp(prefix="corrupt-"))
        try:
            try:
                fn(tmp)
            except expect as exc:
                text = str(exc)
                if fragment and fragment not in text:
                    print(f"  FAIL {name}: {expect.__name__} raised but "
                          f"message {text!r} lacks {fragment!r}")
                    failures += 1
                else:
                    print(f"  ok   {name} -> {expect.__name__}: {text[:90]}")
            except GitHandoffError as exc:
                print(f"  FAIL {name}: expected {expect.__name__}, got "
                      f"{type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"  FAIL {name}: no error raised")
                failures += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"{failures} corruption case(s) failed")
        return 1
    print(f"all {len(CASES)} corruption cases rejected as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
