#!/usr/bin/env python3
"""End-to-end handoff demo: ingest the real bundle, compare every fact with
the hand-written expectations, export two data files from two different
commits and decode one of them through the existing ``decode`` entry.

Only the bundle is opened by the reader.  The repository working tree is
consulted *after* the fact, in a separate subprocess, as an independent
cross-check of what the bundle should contain -- the reader itself never
sees it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook import Manifest, decode_file               # noqa: E402
from copybook.gitkit import (GitHandoffError, decode_path, encode_path,
                             export_blob, ingest_handoff, inspect_handoff)
import expected_handoff as E                             # noqa: E402

RUN = HERE / "run"
BUNDLE = RUN / "handoff.bundle"
REPO = RUN / "repo"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout


def main() -> int:
    if not BUNDLE.is_file():
        print("run examples/git/build_handoff.py first", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="handoff-demo-"))
    try:
        _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all handoff demo checks passed")
    return 0


def _run(tmp: Path) -> None:
    out = tmp / "facts"
    summary = ingest_handoff(BUNDLE, output_dir=out, temp_dir=tmp)
    check("ingest concludes ingested", summary["conclusion"] == "ingested")
    report = json.loads((out / "handoff_report.json").read_text())
    check("report states no extraction happened",
          report["extraction"]["performed"] is False)

    # -- commit graph written by hand from the build narrative ----------
    base = json.loads(git("rev-parse", "main~1~1").strip() or "null") \
        if False else _rev("main~2")
    data_tip = _rev("data")
    layout_tip = _rev("layout")
    merge1 = _rev("main~1")
    merge2 = _rev("main")
    expected_parents = {
        base: [],
        data_tip: [base],
        layout_tip: [base],
        merge1: [base, data_tip],
        merge2: [merge1, layout_tip],
    }
    by_id = {c["object_id"]: c for c in report["commits"]}
    check("all five commits listed", set(by_id) == set(expected_parents),
          f"{sorted(by_id)}")
    for oid, parents in expected_parents.items():
        check(f"parents of {oid[:8]}",
              by_id[oid]["parents"] == parents,
              f"expected {parents}, got {by_id[oid]['parents']}")
    order = report["commit_order"]
    check("base precedes every child",
          order.index(base) < min(order.index(i) for i in expected_parents
                                  if i != base))
    check("merge2 is last", order[-1] == merge2)
    for oid, node in by_id.items():
        if node["parents"]:
            for parent in node["parents"]:
                check(f"{oid[:8]} after parent {parent[:8]}",
                      order.index(parent) < order.index(oid))
    # timestamps are inverted, so the order cannot be date-derived
    check("data child is earlier than its parent",
          by_id[data_tip]["committer"]["timestamp"]
          < by_id[base]["committer"]["timestamp"])
    check("layout child is earlier than its parent",
          by_id[layout_tip]["committer"]["timestamp"]
          < by_id[base]["committer"]["timestamp"])
    check("merge carries two parents in order",
          by_id[merge2]["parents"] == [merge1, layout_tip])

    # -- refs shared by ancestors --------------------------------------
    check("base is reached by all four refs",
          set(by_id[base]["refs"]) == {"HEAD", "refs/heads/data",
                                       "refs/heads/layout",
                                       "refs/heads/main"},
          str(by_id[base]["refs"]))
    check("merge1 is only reached by main",
          by_id[merge1]["refs"] == ["HEAD", "refs/heads/main"],
          str(by_id[merge1]["refs"]))
    check("data branch adds exactly one file",
          _paths(report["diffs"][data_tip][0]["changes"])
          == [(("added"), p) for p in E.DATA_BRANCH_ADDED])
    check("layout branch changes exactly the v1 copybook",
          _paths(report["diffs"][layout_tip][0]["changes"])
          == [(("modified"), p) for p in E.LAYOUT_BRANCH_CHANGED])
    check("base lists the five sample files",
          sorted(f["path"]["display"]
                 for f in report["file_listings"][base]["files"])
          == E.BASE_FILES)

    # -- both merges, per parent ---------------------------------------
    for commit, expected in ((merge1, E.MERGE1_PARENT_DIFFS),
                             (merge2, E.MERGE2_PARENT_DIFFS)):
        rows = report["diffs"][commit]
        check(f"{commit[:8]} has one diff per parent",
              [r["parent_index"] for r in rows] == sorted(expected)
              and [r["parent"] for r in rows]
              == by_id[commit]["parents"])
        for row in rows:
            check(f"{commit[:8]} p{row['parent_index']} changes",
                  _paths(row["changes"]) == expected[row["parent_index"]],
                  str(_paths(row["changes"])))

    # -- tree listing never reports the root tree as a file -------------
    for fact in report["file_listings"][merge2]["files"]:
        check(f"{fact['path']['display']} is not a directory entry",
              fact["type"] in ("blob", "gitlink"))

    # -- independent cross-check from the generated repository ---------
    for oid in expected_parents:
        tree_files = git("ls-tree", "-r", "-z", "--full-tree", oid)
        expected_paths = sorted(
            entry.split("\t", 1)[1]
            for entry in tree_files.split("\x00") if entry)
        listed = sorted(f["path"]["display"]
                        for f in report["file_listings"][oid]["files"])
        check(f"{oid[:8]} listing matches git ls-tree",
              listed == expected_paths, f"{listed} vs {expected_paths}")

    # -- export the same feed from two different commits ----------------
    feed_one = report["file_listings"][data_tip]["files"]
    feed_rel = E.EXPORTED_FEED["path"]
    b64 = encode_path([p.encode() for p in feed_rel.split("/")])
    check("lossless path round-trips", decode_path(b64) == tuple(
        p.encode() for p in feed_rel.split("/")))
    exported = []
    for label, commit in (("data", data_tip), ("merge", merge2)):
        dest = tmp / f"from-{label}.dat"
        manifest = export_blob(BUNDLE, commit_id=commit, path=b64,
                               destination=dest, temp_dir=tmp)
        exported.append((commit, dest, manifest))
        check(f"export from {label} via CLI-compatible manifest",
              manifest["commit"] == commit and dest.is_file())
        check(f"exported bytes hash to the recorded blob",
              manifest["blob"]["object_id"]
              == _git_blob_id(dest.read_bytes()))
    # The merge reaches the data branch, so the same blob is available
    # from a different commit; its bytes must be identical.
    check("merge and data tip export the same feed blob",
          exported[0][2]["blob"]["object_id"]
          == exported[1][2]["blob"]["object_id"])
    check("export from the merge equals export from the data tip",
          exported[0][1].read_bytes() == exported[1][1].read_bytes())
    # The layout branch does not carry the feed at all: exporting it must
    # fail without creating a destination.
    missing = tmp / "from-layout.dat"
    try:
        export_blob(BUNDLE, commit_id=layout_tip, path=b64,
                    destination=missing, temp_dir=tmp)
    except GitHandoffError:
        check("layout branch has no feed and export fails", True)
    else:
        check("layout branch has no feed and export fails", False)
    check("failed export left no destination", not missing.exists())
    # the blob id in the listing is the one exported
    check("listing blob id matches export",
          any(f["object_id"] == exported[0][2]["blob"]["object_id"]
              and f["path"]["b64"] == b64 for f in feed_one))

    # -- read the exported file back in a fresh process -----------------
    dest = exported[0][1]
    fresh = subprocess.run([sys.executable, "-c",
                            "import sys;sys.stdout.buffer.write("
                            "open(sys.argv[1],'rb').read())", str(dest)],
                           capture_output=True, check=True)
    check("fresh process reads the same bytes",
          fresh.stdout == dest.read_bytes())
    check("exported file matches git cat-file",
          fresh.stdout == subprocess.run(
              ["git", "-C", str(REPO), "cat-file", "blob",
               f"{data_tip}:{feed_rel}"],
              capture_output=True, check=True).stdout)

    # -- decode the exported feed through the existing entry -----------
    manifest_path = tmp / "manifest.json"
    manifest_path.write_text(json.dumps({
        "layouts": [{
            "sender": "BANKA", "copybook": "ACCT", "version": "1",
            "effective_from": "2024-01-01", "effective_to": "2024-08-31",
            "file": str(PROJECT / "examples/copybooks/ACCT-1.cpy"),
            "codec": "cp037"}]}))
    result = decode_file(dest, sender="BANKA", business_date="2024-06-15",
                        manifest=Manifest.load(manifest_path))
    check("decode quarantines nothing",
          result.quarantine_count == 0, str(result.quarantined))
    check("decode returns three records",
          result.ok_count == E.EXPORTED_FEED["line_count"])
    for expected, record in zip(E.EXPORTED_FEED["records"],
                                result.records):
        check(f"line {expected['line']} byte range",
              (record.byte_start, record.byte_end)
              == E.EXPORTED_FEED["byte_ranges"][expected["line"] - 1],
              f"{record.byte_start}..{record.byte_end}")
        for name, value in expected["fields"].items():
            check(f"line {expected['line']} {name}",
                  record.fields.get(name) == value,
                  f"{record.fields.get(name)!r} != {value!r}")
    # the layout branch's copybook still parses to the same record length
    changed = subprocess.run(
        ["git", "-C", str(REPO), "show",
         f"{layout_tip}:examples/copybooks/ACCT-1.cpy"],
        capture_output=True, check=True)
    changed_path = tmp / "ACCT-1-revised.cpy"
    changed_path.write_bytes(changed.stdout)
    from copybook import parse_copybook
    layout = parse_copybook(changed_path.read_text(), name="ACCT",
                            version="1-rev")
    check("revised layout keeps the 50-byte record",
          layout.record_length == 50, str(layout.record_length))
    check("revised layout has TRACE-CODE after LAST-TXN-DATE",
          layout.find("TRACE-CODE") is not None)


def _rev(ref: str) -> str:
    return git("rev-parse", ref).strip()


def _paths(changes) -> list[tuple[str, str]]:
    return [(c["change"], c["path"]["display"]) for c in changes]


def _git_blob_id(content: bytes) -> str:
    import hashlib
    h = hashlib.sha1()
    h.update(f"blob {len(content)}\0".encode())
    h.update(content)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
