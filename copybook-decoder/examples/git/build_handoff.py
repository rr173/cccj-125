#!/usr/bin/env python3
"""Build the *real* handoff demo repository, then produce a self-contained
bundle from it.

The repository holds the existing sample data files and copybook layouts
plus the receiving manifest.  Two branches diverge from a common base:
one changes the data (a new account feed), the other changes the layout
(an updated copybook).  The data-changing child is committed with a
timestamp *earlier* than its parent, so no tool can recover parentage by
sorting dates.  The branches are then merged and the whole history is
packed into a self-contained v2 bundle.

``git`` is used here only to generate and inspect the test material, as
the task allows; the reader itself never calls it.  The bundle is the only
thing handed to the reader -- ``run_scenarios.py`` never reads the
repository working tree for objects it checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
WORK = ROOT / "run"
REPO = WORK / "repo"
BUNDLE = WORK / "handoff.bundle"

# Inverted clock: the parent is "later" than its data child on purpose.
T_BASE = "1800000000 +0000"
T_DATA_CHILD = "1600000000 +0000"
T_LAYOUT_CHILD = "1700000000 +0000"
T_MERGE = "1800000100 +0000"

IDENTITY = [
    "-c", "user.name=Handoff Demo",
    "-c", "user.email=demo@example.invalid",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
]


def git(*args: str, env: dict | None = None) -> str:
    full = ["git", *IDENTITY, *args]
    result = subprocess.run(full, cwd=REPO, capture_output=True, text=True,
                            env=env, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def commit(message: str, stamp: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    if env_extra:
        env.update(env_extra)
    git("add", "-A", env=env)
    git("commit", "-q", "--allow-empty", "-m", message, env=env)
    return git("rev-parse", "HEAD")


def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    REPO.mkdir(parents=True)
    git("init", "-q", "-b", "main")

    # --- base: the existing sample material ---------------------------
    for rel in ("examples/copybooks/ACCT-1.cpy",
                "examples/copybooks/ACCT-2.cpy",
                "examples/manifest.json",
                "examples/data/BANKA-20240315.dat",
                "examples/data/BANKA-20241015.dat"):
        src = PROJECT / rel
        dst = REPO / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    base = commit("base: sample data and layouts", T_BASE)

    # --- branch "data": add a new daily feed --------------------------
    git("checkout", "-q", "-b", "data")
    dst = REPO / "examples/data/BANKA-20240615.dat"
    shutil.copyfile(ROOT / "data" / "BANKA-20240615.dat", dst)
    data_tip = commit("data: add 2024-06-15 daily feed", T_DATA_CHILD)

    # --- branch "layout": add TRACE-CODE to the v1 copybook ----------
    git("checkout", "-q", "-b", "layout", base)
    layout_path = REPO / "examples/copybooks/ACCT-1.cpy"
    text = layout_path.read_text(encoding="utf-8")
    text = text.replace(
        "      * ACCT daily account feed -- layout version 1",
        "      * ACCT daily account feed -- layout version 1\n"
        "      * revision 2: TRACE-CODE added, filler shrunk (still 50 "
        "bytes)")
    text = text.replace(
        "           05  LAST-TXN-DATE      PIC 9(8).",
        "           05  LAST-TXN-DATE      PIC 9(8).\n"
        "           05  TRACE-CODE         PIC X(2).")
    text = text.replace(
        "           05  FILLER             PIC X(3).",
        "           05  FILLER             PIC X(1).")
    layout_path.write_text(text, encoding="utf-8")
    layout_tip = commit("layout: add TRACE-CODE to ACCT v1", T_LAYOUT_CHILD)

    # --- merge both branches, with the merge itself later -------------
    git("checkout", "-q", "main")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = T_MERGE
    env["GIT_COMMITTER_DATE"] = T_MERGE
    git("merge", "-q", "--no-ff", "data", "-m", "merge: data", env=env)
    git("merge", "-q", "--no-ff", "layout", "-m", "merge: layout+data",
        env=env)
    merge = git("rev-parse", "HEAD")

    git("bundle", "create", str(BUNDLE), "--all")
    print(f"repository : {REPO}")
    print(f"bundle     : {BUNDLE}")
    print(f"base       : {base}")
    print(f"data tip   : {data_tip}")
    print(f"layout tip : {layout_tip}")
    print(f"merge      : {merge}")


if __name__ == "__main__":
    main()
