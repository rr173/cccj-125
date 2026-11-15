#!/usr/bin/env python3
"""Run every impact-report scenario and check hand-written expectations.

For each scenario built by ``build_scenarios.py`` this runner invokes the
real ``copybook impact`` CLI as a subprocess (so the documented exit
codes are exercised) and compares the JSON report against
``expected_results.py`` line by line.

The runner lives outside the generated data directory and passes all
paths as arguments; it never imports the generated data as code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(HERE))

from build_scenarios import build_all   # noqa: E402
from expected_results import EXPECTED   # noqa: E402


def _run_impact(old_manifest: Path, new_manifest: Path,
                out_dir: Path) -> tuple:
    cmd = [sys.executable, "-m", "copybook", "impact",
           "--old-manifest", str(old_manifest),
           "--new-manifest", str(new_manifest),
           "--out-dir", str(out_dir)]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _ranges(rngs, keys):
    return sorted(tuple(r[k] for k in keys) for r in rngs)


def check_scenario(name: str, root: Path) -> list:
    exp = EXPECTED[name]
    errors: list[str] = []
    scenario = root / "scenarios" / ("main" if name == "existing_outdir"
                                     else name)
    old_manifest = scenario / "old" / "manifest.json"
    new_manifest = scenario / "new" / "manifest.json"
    out_dir = root / name / "report"

    if name == "existing_outdir":
        out_dir.mkdir(parents=True)
        sentinel = out_dir / "previous_review.txt"
        sentinel.write_text("do not overwrite", encoding="utf-8")
        code, _, stderr = _run_impact(old_manifest, new_manifest, out_dir)
        if code != exp["exit_code"]:
            errors.append(f"exit code {code} != expected "
                          f"{exp['exit_code']}")
        if (out_dir / "impact_report.json").exists():
            errors.append("report must not be written into an existing "
                          "directory")
        if sentinel.read_text(encoding="utf-8") != "do not overwrite":
            errors.append("existing directory content was modified")
        return errors

    code, stdout, stderr = _run_impact(old_manifest, new_manifest, out_dir)
    if code != exp["exit_code"]:
        errors.append(f"exit code {code} != expected {exp['exit_code']} "
                      f"(stderr={stderr.strip()})")
    report_path = out_dir / "impact_report.json"
    if not report_path.exists():
        errors.append("impact_report.json missing")
        return errors
    if not (out_dir / "impact_report.txt").exists():
        errors.append("impact_report.txt missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    if report["conclusion"] != exp["conclusion"]:
        errors.append(f"conclusion {report['conclusion']!r} != "
                      f"{exp['conclusion']!r}")

    # problems: (kind, source, sender, copybook, from, to)
    got_problems = sorted(
        (p["kind"], p["source"], p["sender"], p["copybook"],
         p["range"]["from"] if p["range"] else None,
         p["range"]["to"] if p["range"] else None)
        for p in report["problems"])
    want_problems = sorted(exp["problems"])
    if got_problems != want_problems:
        errors.append(f"problems {got_problems} != {want_problems}")
    if "problem_paths" in exp:
        got_paths = sorted(p.get("path") for p in report["problems"])
        if got_paths != sorted(exp["problem_paths"]):
            errors.append(f"problem paths {got_paths} != "
                          f"{sorted(exp['problem_paths'])}")

    # per-sender status and date ranges
    for sender, want in exp["senders"].items():
        block = next((b for b in report["senders"]
                      if b["sender"] == sender), None)
        if block is None:
            errors.append(f"sender {sender} missing from report")
            continue
        if block["status"] != want["status"]:
            errors.append(f"{sender} status {block['status']!r} != "
                          f"{want['status']!r}")
        got_affected = _ranges(block["affected_ranges"],
                               ("copybook", "from", "to", "old_version",
                                "new_version", "conclusion"))
        if got_affected != sorted(want["affected"]):
            errors.append(f"{sender} affected {got_affected} != "
                          f"{sorted(want['affected'])}")
        got_unaffected = []
        got_unresolvable = []
        for key in block["keys"]:
            got_unaffected.extend(
                (key["copybook"], r["from"], r["to"], r["version"])
                for r in key["unaffected_ranges"])
            got_unresolvable.extend(
                (r["from"], r["to"], r["reason"])
                for r in key["unresolvable_ranges"])
        if sorted(got_unaffected) != sorted(want.get("unaffected", [])):
            errors.append(f"{sender} unaffected {sorted(got_unaffected)} "
                          f"!= {sorted(want.get('unaffected', []))}")
        if sorted(got_unresolvable) != sorted(
                want.get("unresolvable", [])):
            errors.append(f"{sender} unresolvable "
                          f"{sorted(got_unresolvable)} != "
                          f"{sorted(want.get('unresolvable', []))}")

    # field-level comparison
    if "comparison" in exp:
        want_cmp = exp["comparison"]
        sender, copybook = want_cmp["key"]
        block = next(b for b in report["senders"]
                     if b["sender"] == sender)
        key = next(k for k in block["keys"]
                   if k["copybook"] == copybook)
        if len(key["comparisons"]) != 1:
            errors.append(f"expected exactly one comparison, got "
                          f"{len(key['comparisons'])}")
            return errors
        cmp = key["comparisons"][0]
        got_fields = {f["path"]: f["category"] for f in cmp["fields"]}
        if got_fields != want_cmp["fields"]:
            for path in sorted(set(got_fields) | set(want_cmp["fields"])):
                got = got_fields.get(path)
                want = want_cmp["fields"].get(path)
                if got != want:
                    errors.append(f"field {path}: {got} != expected "
                                  f"{want}")
        lengths = (cmp["record_length"]["old"],
                   cmp["record_length"]["new"])
        if lengths != want_cmp["record_length"]:
            errors.append(f"record length {lengths} != "
                          f"{want_cmp['record_length']}")
        if cmp["conclusion"] != want_cmp["conclusion"]:
            errors.append(f"comparison conclusion {cmp['conclusion']!r} "
                          f"!= {want_cmp['conclusion']!r}")
        accounted = sum(i["bytes"]
                        for i in cmp["record_length"]["explanation"])
        if accounted != lengths[1] - lengths[0]:
            errors.append("record length explanation does not add up: "
                          f"{accounted} vs {lengths[1] - lengths[0]}")
        for path, (old_bytes, new_bytes) in want_cmp["bytes"].items():
            field = next((f for f in cmp["fields"]
                          if f["path"] == path), None)
            if field is None:
                errors.append(f"field {path} missing from comparison")
                continue
            got_old = field.get("old", {}).get("bytes")
            got_new = field.get("new", {}).get("bytes")
            if got_old != old_bytes or got_new != new_bytes:
                errors.append(f"{path} bytes ({got_old}, {got_new}) != "
                              f"({old_bytes}, {new_bytes})")
        got_clusters = sorted(
            (c["target"], c["bytes"], c["old_views"], c["new_views"])
            for c in cmp["view_clusters"])
        want_clusters = sorted(
            (t, b, ov, nv)
            for t, b, ov, nv in want_cmp["view_clusters"])
        if got_clusters != want_clusters:
            errors.append(f"view clusters {got_clusters} != "
                          f"{want_clusters}")
    return errors


def main() -> int:
    root = HERE / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    names = build_all(root / "scenarios")
    assert set(names) <= set(EXPECTED) | {"main"}, names

    all_ok = True
    for name in EXPECTED:
        errors = check_scenario(name, root)
        if errors:
            all_ok = False
            print(f"FAIL  {name}")
            for error in errors:
                print(f"        - {error}")
        else:
            print(f"PASS  {name}")
    print()
    print("scenarios:", "ALL OK" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
