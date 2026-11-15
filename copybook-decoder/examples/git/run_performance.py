#!/usr/bin/env python3
"""Resource-bound check: a bundle with many unrelated blobs and the
required files is ingested while measuring peak RSS and staging bytes.

The assertion is structural, not aspirational: peak RSS must stay far
below the total decompressed size of the pack, and the staging directory
must be gone after the run (and only this run's own staging removed).
A second run with a deliberately tiny single-object limit must fail
without leaving a report or a staging directory.

Usage: python3 examples/git/run_performance.py
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(PROJECT))

from copybook.gitkit import (GitHandoffError, ingest_handoff,
                             read_bundle_layout)

from tests.gitkit import packutil as P

#: Total decompressed object bytes in the generated pack.
TARGET_OBJECT_BYTES = 96 * 1024 * 1024
UNRELATED_BLOBS = 2000
FILE_SIZE = 48 * 1024


def _git_oid(obj_type: int, content: bytes) -> str:
    return P.object_id(obj_type, content)


def build_large_bundle(tmp: Path) -> tuple[Path, int, int]:
    unrelated = []
    total = 0
    for i in range(UNRELATED_BLOBS):
        content = (f"unrelated blob {i:04d}\n".encode()
                   + bytes([i % 251]) * FILE_SIZE)
        unrelated.append(content)
        total += len(content)
    wanted = [
        b"line one of the wanted feed\n",
        b"line two of the wanted feed\n",
        b"line three of the wanted feed\n",
    ]
    total += sum(len(w) for w in wanted)

    wanted_ids = [_git_oid(P.OBJ_BLOB, w) for w in wanted]
    tree = P.tree_bytes([
        ("100644", b"wanted-01.dat", wanted_ids[0]),
        ("100644", b"wanted-02.dat", wanted_ids[1]),
        ("100755", b"wanted-03.dat", wanted_ids[2]),
    ] + [("100644", f"noise-{i:04d}.bin".encode(),
          _git_oid(P.OBJ_BLOB, content))
         for i, content in enumerate(unrelated)])
    tree_id = _git_oid(P.OBJ_TREE, tree)
    commit = P.commit_bytes(
        tree_id, [], "Fixture Writer <fixture@example.invalid> "
        "1700000000 +0000",
        "Fixture Writer <fixture@example.invalid> 1700000000 +0000",
        "large fixture\n")
    commit_id = _git_oid(P.OBJ_COMMIT, commit)

    entries = [{"type": P.OBJ_BLOB, "content": content}
               for content in unrelated]
    entries += [{"type": P.OBJ_BLOB, "content": w} for w in wanted]
    entries.append({"type": P.OBJ_TREE, "content": tree})
    entries.append({"type": P.OBJ_COMMIT, "content": commit})
    pack = P.build_pack(entries)
    bundle = P.build_bundle([f"{commit_id} refs/heads/main"], pack)
    path = tmp / "large.bundle"
    path.write_bytes(bundle)
    return path, total, len(pack)


def build_in_subprocess(tmp: Path) -> tuple[Path, int, int]:
    """Generate the fixture in a child process.

    Building the pack in-process would retain the generator's own buffers
    and make the child's RSS measurement meaningless.
    """
    bundle = tmp / "large.bundle"
    script = (
        "import json,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(PROJECT)!r})\n"
        "from examples.git.run_performance import build_large_bundle\n"
        f"path, total, pack = build_large_bundle(Path({str(tmp)!r}))\n"
        "print(json.dumps([str(path), total, pack]))\n"
    )
    env = dict(os.environ)
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, env=env,
                            cwd=str(PROJECT))
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("fixture build failed")
    path, total, pack = json.loads(result.stdout.strip().splitlines()[-1])
    return Path(path), total, pack


def run_ingest_isolation(bundle: Path, out: Path, tmp: Path,
                         stage: Path) -> dict:
    """Run the ingest in a fresh interpreter and report its peak RSS."""
    script = (
        "import json,sys,resource\n"
        "from copybook.gitkit import ingest_handoff\n"
        f"summary = ingest_handoff({str(bundle)!r}, output_dir={str(out)!r}, "
        f"temp_dir={str(stage)!r})\n"
        "print(json.dumps({'peak_rss_kb': resource.getrusage("
        "resource.RUSAGE_SELF).ru_maxrss, 'summary': summary}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT)
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, env=env,
                            cwd=str(tmp))
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit("ingest subprocess failed")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="perf-"))
    failures = []
    try:
        bundle, object_bytes, pack_bytes = build_in_subprocess(tmp)
        layout = read_bundle_layout(bundle)
        # A neighbouring file in the staging base must survive.
        stage = tmp / "stage"
        stage.mkdir()
        neighbour = stage / "neighbour.txt"
        neighbour.write_text("do not touch")
        out = tmp / "facts"
        aged = run_ingest_isolation(bundle, out, tmp, stage)
        peak_kb = aged["peak_rss_kb"]
        peak_mb = peak_kb / 1024
        report = json.loads((out / "handoff_report.json").read_text())
        print(f"bundle bytes       : {layout.size}")
        print(f"pack bytes         : {pack_bytes}")
        print(f"decompressed total : {object_bytes} bytes "
              f"({object_bytes / (1024 * 1024):.1f} MiB)")
        print(f"objects            : {report['counts']['object_count']}")
        print(f"peak RSS           : {peak_kb} KiB ({peak_mb:.1f} MiB)")
        print(f"staged bytes       : "
              f"{report['resources']['staged_bytes']} (spilled to disk)")
        print(f"staging after run  : {list(stage.iterdir())}")

        # Not the whole bundle, nor all decompressed objects, may sit in
        # Python memory: peak RSS must be well under the object bytes.
        if peak_kb * 1024 >= object_bytes // 2:
            failures.append(
                "peak RSS is not clearly below half the decompressed size")
        if not neighbour.is_file() or neighbour.read_text() != "do not touch":
            failures.append("neighbouring staging file was touched")
        if [p for p in stage.iterdir() if p != neighbour]:
            failures.append("run staging was not cleaned up")
        # Staged bytes are the object total plus the tree and the commit;
        # the pack itself is far smaller, so nothing is resident in RAM.
        staged = report["resources"]["staged_bytes"]
        if not object_bytes <= staged <= object_bytes + 1024 * 1024:
            failures.append(
                f"staged bytes {staged} are not the object total "
                f"{object_bytes} plus the trees/commit")
        # The wanted feed is listed, the noise is not selected for reading.
        listed = {f["path"]["display"]
                  for f in report["file_listings"][
                      report["commit_order"][0]]["files"]}
        for name in ("wanted-01.dat", "wanted-02.dat", "wanted-03.dat"):
            if name not in listed:
                failures.append(f"{name} missing from listing")

        # A tiny single-object limit must refuse, leave no report and no
        # staging directory of its own.
        out2 = tmp / "facts-limited"
        stage2 = tmp / "stage2"
        stage2.mkdir()
        try:
            ingest_handoff(bundle, output_dir=out2, temp_dir=stage2,
                           max_object_bytes=1024)
        except GitHandoffError as exc:
            print(f"limit refusal      : {type(exc).__name__}: "
                  f"{str(exc)[:90]}")
        else:
            failures.append("tiny object limit did not refuse")
        if out2.exists():
            failures.append("limit refusal left an output directory")
        if list(stage2.iterdir()):
            failures.append("limit refusal left staging behind")
        if out2.exists() and (out2 / "commits.jsonl").exists():
            failures.append("limit refusal left a partial report")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("resource-bound checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
