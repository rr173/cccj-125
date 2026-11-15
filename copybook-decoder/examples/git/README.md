# Offline Git handoff demo

This directory holds the executable demonstration for `copybook git-ingest`,
`git-export` and `git-query`. `git` is used **only** to generate and
independently cross-check test material; the reader itself is pure Python
standard library and never shells out.

```bash
python3 examples/git/build_handoff.py     # git repo + self-contained bundle
python3 examples/git/run_scenarios.py     # ingest, compare, export, decode
python3 examples/git/run_edge_cases.py    # hand-built PACK edge cases
python3 examples/git/run_corruptions.py   # damaged copies, re-sealed
python3 examples/git/run_performance.py   # peak RSS, staging, limits
```

## Real handoff story

`build_handoff.py` creates `run/repo` from the existing sample data and
layouts, then:

1. base commit on `main`;
2. branch `data` adds `examples/data/BANKA-20240615.dat`, committed with a
   timestamp **earlier than its parent**;
3. branch `layout` adds `TRACE-CODE` to `ACCT-1.cpy`, keeping the record at
   50 bytes, also with an early timestamp;
4. two merges (`data`, then `layout+data`) and `git bundle create --all`.

`expected_handoff.py` writes by hand the parent graph, each branch's file
version, both merges' per-parent change lists and the decoded fields of the
exported feed. `run_scenarios.py` ingests the bundle, compares every item,
exports the feed from two commits, re-reads the bytes in a fresh process,
and feeds one export into the existing `decode` entry to check fields and
byte ranges. The repository working tree is consulted only afterwards, as
an independent cross-check (`git ls-tree`, `git cat-file`).

## Hand-built PACK

`run_edge_cases.py` builds its pack byte by byte: REF_DELTA entries precede
their bases, a REF delta is based on another REF delta, an OFS delta shares
the same base under a different path, and a merge has two unrelated
parents. It covers shared ancestors across refs, content+mode change,
mode-only change, deletion, file→directory, newline and non-UTF-8 names,
symlinks, gitlinks, one blob at two paths and one tree mounted twice.

## Corrupt copies

`run_corruptions.py` damages a copy and, where needed, recomputes the outer
pack/bundle checksum so the inner check is actually reached: delta copy
out of range, missing base, declared size too small/large, tree entry to a
missing object, tree entry with a mismatched type, a commit parent that is
a blob, an extra byte before the trailer, a corrupt trailer and trailing
bytes after it, and a corrupt zlib stream.

## Resource bounds

`run_performance.py` builds (in a child process, so the generator's buffers
do not pollute the measurement) a pack with ~96 MiB of decompressed
content across 2000 unrelated blobs, then measures the ingest child's peak
RSS. Peak RSS stays around 40 MiB, the full decompressed content is spilled
to the staging directory, staging is removed afterwards, a neighbouring
file in the staging base is untouched, and a tiny `--max-object-bytes`
refusal leaves neither a report nor staging behind.
