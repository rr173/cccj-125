#!/usr/bin/env python3
"""Coverage demo for tree/file edge cases and per-parent merge diffs.

Builds one hand-made bundle (no git binary) containing:

* a branch structure where a merge has two unrelated parents;
* a content change and a mode-only change (and one file that changes
  both);
* a deletion, and a file that becomes a directory;
* names with a newline, a non-UTF-8 byte and a regular UTF-8 name;
* a symlink and a gitlink;
* one blob referenced by two different paths;
* one tree shared by two commits (and mounted under two prefixes);
* two refs sharing an ancestor.

Every expectation is written as a literal and compared with the reader's
report.  Run from the project root:

    python3 examples/git/run_edge_cases.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.gitkit import (encode_path, export_blob, inspect_handoff,
                             read_bundle_layout)
from tests.gitkit import packutil as P

AUTHOR = "Edge Writer <edge@example.invalid> 1700000000 +0000"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def oid(kind: str, content: bytes) -> str:
    return P.object_id({"blob": P.OBJ_BLOB, "tree": P.OBJ_TREE,
                        "commit": P.OBJ_COMMIT}[kind], content)


def make_objects():
    """Every object payload, written once.

    Returns ``(blobs, ids, gitlink, trees, commits)`` where ``trees`` maps
    a tree id to its payload and ``commits`` maps a label to
    ``(tree_id, commit_payload, commit_id)``.
    """
    blobs = {
        "shared": b"shared content\n",
        "changed": b"version one\n",
        "changed2": b"version two with more text\n",
        "mode1": b"#!/bin/sh\necho one\n",
        "gone": b"will be deleted\n",
        "becomes_dir": b"i was a file\n",
        "link": b"shared.txt",           # symlink target text
        "nl": b"newline name\n",
        "nonutf8": b"non-utf8 name\n",
    }
    ids = {k: oid("blob", v) for k, v in blobs.items()}
    gitlink = "ab" * 20

    mounted = P.tree_bytes([("100644", b"inner.txt", ids["shared"])])
    mounted_id = oid("tree", mounted)

    trees = {mounted_id: mounted}

    v1 = P.tree_bytes([
        ("100644", b"shared.txt", ids["shared"]),
        ("100644", b"changed.txt", ids["changed"]),
        ("100755", b"run.sh", ids["mode1"]),
        ("100644", b"gone.txt", ids["gone"]),
        ("100644", b"node", ids["becomes_dir"]),
        ("100644", b"line\nbreak", ids["nl"]),
        ("100644", b"\xffraw", ids["nonutf8"]),
        ("120000", b"link", ids["link"]),
        ("160000", b"vendor", gitlink),
        ("40000", b"mounted", mounted_id),
    ])
    v1_id = oid("tree", v1)
    trees[v1_id] = v1

    v2 = P.tree_bytes([
        ("100644", b"shared.txt", ids["shared"]),
        ("100755", b"changed.txt", ids["changed2"]),
        ("100644", b"run.sh", ids["mode1"]),
        ("40000", b"node", mounted_id),
        ("100644", b"line\nbreak", ids["nl"]),
        ("100644", b"\xffraw", ids["nonutf8"]),
        ("120000", b"link", ids["link"]),
        ("160000", b"vendor", gitlink),
        ("40000", b"mounted", mounted_id),
    ])
    v2_id = oid("tree", v2)
    trees[v2_id] = v2

    v3 = P.tree_bytes([
        ("100644", b"changed.txt", ids["changed"]),
        ("100755", b"run.sh", ids["mode1"]),
        ("100644", b"gone.txt", ids["gone"]),
        ("100644", b"node", ids["becomes_dir"]),
        ("100644", b"line\nbreak", ids["nl"]),
        ("100644", b"\xffraw", ids["nonutf8"]),
        ("120000", b"link", ids["link"]),
        ("160000", b"vendor", gitlink),
        ("40000", b"mounted", mounted_id),
        ("100644", b"added.txt", ids["changed"]),
    ])
    v3_id = oid("tree", v3)
    trees[v3_id] = v3

    merge = P.tree_bytes([
        ("100644", b"shared.txt", ids["shared"]),
        ("100755", b"changed.txt", ids["changed2"]),
        ("100644", b"run.sh", ids["mode1"]),
        ("40000", b"node", mounted_id),
        ("100644", b"line\nbreak", ids["nl"]),
        ("100644", b"\xffraw", ids["nonutf8"]),
        ("120000", b"link", ids["link"]),
        ("160000", b"vendor", gitlink),
        ("40000", b"mounted", mounted_id),
        ("100644", b"added.txt", ids["changed"]),
    ])
    merge_id = oid("tree", merge)
    trees[merge_id] = merge

    c1 = P.commit_bytes(v1_id, [], AUTHOR, AUTHOR, "root\n")
    c1_id = oid("commit", c1)
    older = "Edge Writer <edge@example.invalid> 1600000000 +0000"
    c2 = P.commit_bytes(v2_id, [c1_id], older, older, "main side\n")
    c2_id = oid("commit", c2)
    c3 = P.commit_bytes(v3_id, [c1_id], AUTHOR, AUTHOR, "other side\n")
    c3_id = oid("commit", c3)
    c4 = P.commit_bytes(merge_id, [c2_id, c3_id], AUTHOR, AUTHOR, "merge\n")
    c4_id = oid("commit", c4)
    commits = {
        "v1": (v1_id, c1, c1_id),
        "v2": (v2_id, c2, c2_id),
        "v3": (v3_id, c3, c3_id),
        "merge": (merge_id, c4, c4_id),
    }
    return blobs, ids, gitlink, trees, commits


def build_bundle(tmp: Path) -> dict:
    blobs, ids, gitlink, trees, commits = make_objects()
    entries = [{"type": P.OBJ_BLOB, "content": v} for v in blobs.values()]
    entries += [{"type": P.OBJ_TREE, "content": payload}
                for payload in trees.values()]
    entries += [{"type": P.OBJ_COMMIT, "content": commit_bytes}
                for _, commit_bytes, _ in commits.values()]
    pack = P.build_pack(entries)
    lines = [f"{commits['v2'][2]} refs/heads/main",
             f"{commits['v3'][2]} refs/heads/side",
             f"{commits['merge'][2]} refs/heads/merged",
             f"{commits['merge'][2]} HEAD"]
    path = tmp / "edge.bundle"
    path.write_bytes(P.build_bundle(lines, pack))
    return {"path": path, "ids": ids, "gitlink": gitlink,
            "commits": commits}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="edge-"))
    try:
        material = build_bundle(tmp)
        report = inspect_handoff(material["path"], temp_dir=tmp)
        ids = material["ids"]
        commits = material["commits"]
        c1, c2, c3, c4 = (commits[k][2] for k in ("v1", "v2", "v3",
                                                  "merge"))

        # -- graph: merge has both branches, order follows parents -------
        order = report["commit_order"]
        check("root precedes both branches",
              order.index(c1) < order.index(c2)
              and order.index(c1) < order.index(c3))
        check("merge follows both parents",
              order.index(c2) < order.index(c4)
              and order.index(c3) < order.index(c4))
        nodes = {c["object_id"]: c for c in report["commits"]}
        check("merge parents kept in order",
              nodes[c4]["parents"] == [c2, c3])
        check("child predates parent but is ordered after",
              nodes[c2]["committer"]["timestamp"]
              < nodes[c1]["committer"]["timestamp"])
        # Every ref that reaches the root is recorded (the merge ref also
        # reaches it through both parents).
        check("ancestors list every reaching ref",
              nodes[c1]["refs"] == ["HEAD", "refs/heads/main",
                                    "refs/heads/merged",
                                    "refs/heads/side"],
              str(nodes[c1]["refs"]))

        # -- listing: kinds and raw names -------------------------------
        listing = {f["path"]["display"]: f
                   for f in report["file_listings"][c1]["files"]}
        check("regular file kind", listing["shared.txt"]["kind"] == "file")
        check("executable kind", listing["run.sh"]["kind"] == "executable")
        check("symlink kind", listing["link"]["kind"] == "symlink")
        check("gitlink kind", listing["vendor"]["kind"] == "gitlink")
        check("gitlink id preserved",
              listing["vendor"]["object_id"] == material["gitlink"])
        check("gitlink has no size", listing["vendor"]["size"] is None)
        check("newline name survives as base64",
              encode_path([b"line\nbreak"])
              in {f["path"]["b64"]
                  for f in report["file_listings"][c1]["files"]})
        check("non-UTF-8 name survives as base64",
              encode_path([b"\xffraw"])
              in {f["path"]["b64"]
                  for f in report["file_listings"][c1]["files"]})
        check("root tree is not listed",
              "" not in [f["path"]["b64"]
                         for f in report["file_listings"][c1]["files"]])
        # same blob at two paths = two file facts
        same = [f for f in report["file_listings"][c1]["files"]
                if f["object_id"] == ids["shared"]]
        check("one blob, two file facts", len(same) == 2,
              str([f["path"]["display"] for f in same]))
        # shared tree mounted under two commits keeps the right prefix
        mounted = {f["path"]["display"]
                   for f in report["file_listings"][c1]["files"]
                   if f["path"]["display"].endswith("inner.txt")}
        check("shared tree mount prefix is per-call", mounted ==
              {"mounted/inner.txt"}, str(mounted))

        # -- diff v2 against v1 (per parent) ----------------------------
        rows = report["diffs"][c2]
        check("v2 has exactly one parent row", len(rows) == 1)
        changes = {(c["change"], c["path"]["display"]): c
                   for c in rows[0]["changes"]}
        check("content+mode change shows both flags",
              changes[("modified", "changed.txt")]["content_changed"]
              and changes[("modified", "changed.txt")]["mode_changed"])
        check("mode-only change",
              changes[("modified", "run.sh")]["mode_changed"]
              and not changes[("modified", "run.sh")]["content_changed"])
        check("deletion reported", ("deleted", "gone.txt") in changes)
        check("file -> directory: leaf deleted",
              ("deleted", "node") in changes)
        check("file -> directory: nested leaf added",
              ("added", "node/inner.txt") in changes)

        # -- merge diffed against each parent independently -------------
        merged = report["diffs"][c4]
        check("merge has two parent rows",
              [r["parent_index"] for r in merged] == [0, 1]
              and [r["parent"] for r in merged] == [c2, c3])
        vs_main = {(c["change"], c["path"]["display"])
                   for c in merged[0]["changes"]}
        vs_side = {(c["change"], c["path"]["display"])
                   for c in merged[1]["changes"]}
        check("vs main: added.txt appears", ("added", "added.txt") in vs_main)
        check("vs main: shared.txt kept", ("deleted", "shared.txt")
              not in vs_main)
        check("vs side: added.txt kept", ("deleted", "added.txt")
              not in vs_side)
        check("vs side: changed.txt modified",
              ("modified", "changed.txt") in vs_side)
        check("vs side: shared.txt added back",
              ("added", "shared.txt") in vs_side)
        check("no synthetic combined parent",
              all(r["parent"] is not None for r in merged))

        # -- export the shared blob from both paths ---------------------
        for path in ("shared.txt", "mounted/inner.txt"):
            dest = tmp / (path.replace("/", "_") + ".out")
            manifest = export_blob(
                material["path"], commit_id=c1,
                path=encode_path([p.encode() for p in path.split("/")]),
                destination=dest, temp_dir=tmp)
            check(f"export {path} byte-identical",
                  dest.read_bytes() == b"shared content\n")
            check(f"export {path} records the blob",
                  manifest["blob"]["object_id"] == ids["shared"])
        # a symlink and a gitlink are queryable but not exportable
        for path, label in (("link", "symlink"), ("vendor", "gitlink")):
            dest = tmp / f"{path}.out"
            try:
                export_blob(material["path"], commit_id=c1,
                            path=encode_path([path.encode()]),
                            destination=dest, temp_dir=tmp)
            except Exception:
                check(f"{label} export refused", not dest.exists())
            else:
                check(f"{label} export refused", False)
        # the root tree is not a file
        try:
            export_blob(material["path"], commit_id=c1, path="",
                        destination=tmp / "root.out", temp_dir=tmp)
        except Exception:
            check("root tree export refused", True)
        else:
            check("root tree export refused", False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} edge-case check(s) failed")
        return 1
    print("all edge-case checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
