"""Large-file (>256 MiB) streaming rewrite performance harness.

This is not a unit test of correctness (that is covered elsewhere) but a
parameterised end-to-end run proving the engine never holds the whole
file in memory:

* the original is generated in bounded chunks, written to disk, and its
  SHA-256 accumulated without retaining the bytes;
* sparse corrections target the first, a middle and the last record;
* after the rewrite the two files are compared position by position in
  chunks and the resulting difference set is checked against the
  evidence intervals independently.

Run directly to build and exercise a file larger than 256 MiB::

    python3 tests/rewrite/run_performance.py --size-mib 270 --workdir ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copybook.rewrite import rewrite_original
from tests.rewrite.fixtures import LAYOUT_A, write_layouts

RECORD_LENGTH = 44


def _rssi_kib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def build_large_original(path: Path, record_count: int,
                         chunk_records: int = 4096) -> dict:
    """Stream-write *record_count* fixed records plus sparse markers.

    Returns the digest bookkeeping needed to build a valid order.
    """
    template = bytearray(RECORD_LENGTH)
    template[0:8] = "LARGE001".encode("cp037")
    template[8:20] = b"\x40" * 12
    template[43:44] = "1".encode("cp037")
    full = hashlib.sha256()
    record_digests: dict[int, str] = {}
    target_indices = sorted({0, record_count // 2, record_count - 1})
    target_set = set(target_indices)
    written = 0
    with path.open("wb") as fh:
        batch: list[bytes] = []
        for idx in range(record_count):
            rec = bytearray(template)
            # embed an index-independent body so records are uniform,
            # but give target records a distinct marker we can check
            if idx in target_set:
                rec[20:24] = f"T{idx % 1000:03d}".encode("cp037")[:4]
            raw = bytes(rec)
            if idx in target_set:
                record_digests[idx] = hashlib.sha256(raw).hexdigest()
            batch.append(raw)
            if len(batch) >= chunk_records:
                block = b"".join(batch)
                fh.write(block)
                full.update(block)
                written += len(block)
                batch.clear()
        if batch:
            block = b"".join(batch)
            fh.write(block)
            full.update(block)
            written += len(block)
    return {
        "sha256": full.hexdigest(),
        "length": written,
        "target_indices": target_indices,
        "record_digests": record_digests,
    }


def build_order(workdir: Path, info: dict) -> Path:
    order = {
        "source_sha256": info["sha256"],
        "layouts": {
            "A": {"copybook": "ORDA.cpy", "name": "ORD-A", "version": "1",
                  "codec": "cp037"}},
        "records": [],
    }
    for idx in info["target_indices"]:
        start = idx * RECORD_LENGTH
        order["records"].append({
            "byte_start": start,
            "byte_end": start + RECORD_LENGTH,
            "layout": "A",
            "record_sha256": info["record_digests"][idx],
            "edits": [{
                "path": ["FLAG"],
                # original FLAG byte is EBCDIC '1' = 0xf1
                "expected_hex": "f1",
                "value": "8",
            }],
        })
    p = workdir / "large_order.json"
    p.write_text(json.dumps(order), encoding="utf-8")
    return p


def verify_outputs(source: Path, rewritten: Path, evidence: dict,
                   info: dict) -> dict:
    """Independent chunked byte comparison and interval cross-check."""
    size = source.stat().st_size
    assert rewritten.stat().st_size == size
    diff_positions: set[int] = set()
    chunk = 4 * 1024 * 1024
    pos = 0
    with source.open("rb") as a, rewritten.open("rb") as b:
        while True:
            ba = a.read(chunk)
            bb = b.read(chunk)
            if not ba:
                assert bb == b""
                break
            assert len(ba) == len(bb)
            for offset, (x, y) in enumerate(zip(ba, bb)):
                if x != y:
                    diff_positions.add(pos + offset)
            pos += len(ba)
    expected = {idx * RECORD_LENGTH + 43 for idx in info["target_indices"]}
    assert diff_positions == expected, (
        f"diff set mismatch: extra={diff_positions - expected}, "
        f"missing={expected - diff_positions}")
    interval_positions: set[int] = set()
    for iv in evidence["change_intervals"]:
        interval_positions.update(range(iv["byte_start"], iv["byte_end"]))
    assert interval_positions == diff_positions
    assert evidence["counts"]["changed_bytes"] == len(diff_positions)
    assert evidence["counts"]["requested_fields"] == len(
        info["target_indices"])
    # re-hash the output independently
    h = hashlib.sha256()
    with rewritten.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    assert h.hexdigest() == evidence["output"]["sha256"]
    return {"diff_count": len(diff_positions),
            "positions": sorted(diff_positions)}


def run(size_mib: int, workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    copybook_dir = workdir / "copybooks"
    write_layouts(copybook_dir)
    record_count = (size_mib * 1024 * 1024) // RECORD_LENGTH
    source = workdir / "large_original.bin"
    staging = workdir / "staging"
    delivery = workdir / "delivery"

    rss_before = _rssi_kib()
    t0 = time.perf_counter()
    info = build_large_original(source, record_count)
    build_seconds = time.perf_counter() - t0
    rss_after_build = _rssi_kib()
    actual_size = source.stat().st_size

    order_path = build_order(workdir, info)
    t0 = time.perf_counter()
    evidence = rewrite_original(
        order_path, source_path=source, output_dir=delivery,
        temp_dir=staging, copybook_base=copybook_dir,
        chunk_size=1024 * 1024)
    rewrite_seconds = time.perf_counter() - t0
    rss_peak = _rssi_kib()

    t0 = time.perf_counter()
    verification = verify_outputs(
        source, delivery / "original.rewritten.bin", evidence, info)
    verify_seconds = time.perf_counter() - t0

    report = {
        "requested_size_mib": size_mib,
        "record_count": record_count,
        "actual_size_bytes": actual_size,
        "actual_size_mib": actual_size / (1024 * 1024),
        "target_records": info["target_indices"],
        "build_seconds": round(build_seconds, 3),
        "rewrite_seconds": round(rewrite_seconds, 3),
        "verify_seconds": round(verify_seconds, 3),
        "rss_before_kib": rss_before,
        "rss_after_build_kib": rss_after_build,
        "rss_peak_kib": rss_peak,
        "rss_peak_mib": round(rss_peak / 1024, 2),
        "file_resident_ratio": round(
            (rss_peak * 1024) / max(actual_size, 1), 4),
        "verification": verification,
        "evidence_counts": evidence["counts"],
    }
    (workdir / "performance_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def main_cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=270)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)
    report = run(args.size_mib, Path(args.workdir))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
