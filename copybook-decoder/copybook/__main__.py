"""Command line interface: decode data files, audit batch handoffs,
report the impact of layout changes between two receiving manifests, and
generate de-identified samples from decoded records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import (AuditConfig, AuditConfigError, AuditInputError,
                    BUSINESS_REJECTED, run_audit)
from .errors import CopybookError
from .impact import (COMPATIBLE, INCOMPATIBLE, ImpactError,
                     run_impact_report)
from .manifest import Manifest
from .mask import MaskError, MaskKey, MaskPolicy, run_mask
from .pipeline import decode_file

# Exit codes for the audit subcommand.
EXIT_OK = 0                 # structure valid, every batch accepted
EXIT_BUSINESS_REJECT = 1   # audited; at least one batch/file is refused
EXIT_CANNOT_VALIDATE = 2   # input evidence or configuration is unusable

# Exit codes for the impact subcommand.
EXIT_IMPACT_COMPATIBLE = 0     # every affected date range stays compatible
EXIT_IMPACT_INCOMPATIBLE = 1   # report written; breaking changes found
EXIT_IMPACT_UNVERIFIABLE = 2   # problems prevent a complete comparison

# Exit codes for the mask subcommand.
EXIT_MASK_OK = 0               # masked dataset delivered
EXIT_MASK_FAILED = 2           # input/policy/key/conflict/output failure


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="copybook",
        description="Decode fixed-width EBCDIC data files using versioned "
                    "copybook layouts, and audit FHEAD/BHEAD/DETAIL/BTAIL/"
                    "FTAIL batch handoffs.")
    sub = parser.add_subparsers(dest="command", required=True)

    dec = sub.add_parser("decode", help="decode one data file")
    dec.add_argument("file", help="data file to decode")
    dec.add_argument("--manifest", required=True, help="manifest JSON path")
    dec.add_argument("--sender", required=True, help="sender code")
    dec.add_argument("--date", required=True,
                     help="business date, YYYY-MM-DD")
    dec.add_argument("--copybook", help="copybook name (disambiguation)")
    dec.add_argument("--out", help="write decoded records as JSONL here")
    dec.add_argument("--quarantine-dir",
                     help="directory for quarantined lines + index")

    aud = sub.add_parser(
        "audit",
        help="validate a batch handoff from a decoded JSONL and a "
             "quarantine index")
    aud.add_argument("records",
                     help="normal-record JSONL produced by write_jsonl")
    aud.add_argument("quarantine_index",
                     help="quarantine index JSONL produced by "
                          "export_quarantine (may be an empty file)")
    aud.add_argument("--config", required=True,
                     help="audit config JSON: field paths + category table")
    aud.add_argument("--expected-source", required=True,
                     help="source_file every evidence row must declare")
    aud.add_argument("--expected-lines", required=True, type=int,
                     help="number of physical lines in the source file")
    aud.add_argument("--out-dir", required=True,
                     help="new delivery directory (must not exist yet)")
    aud.add_argument("--temp-dir", required=True,
                     help="base directory for SQLite disk staging; only the "
                          "unique subdirectory created for this run is "
                          "cleaned up")

    imp = sub.add_parser(
        "impact",
        help="compare two receiving manifests and report which senders "
             "and dates a layout change impacts (no data files are read)")
    imp.add_argument("--old-manifest", required=True,
                     help="manifest JSON currently in production")
    imp.add_argument("--new-manifest", required=True,
                     help="candidate manifest JSON introducing the new "
                          "copybook version")
    imp.add_argument("--out-dir", required=True,
                     help="new report directory (must not exist yet)")

    msk = sub.add_parser(
        "mask",
        help="generate a de-identified sample from decoded JSONL records "
             "(does not read the original EBCDIC files)")
    msk.add_argument("inputs", nargs="+",
                     help="decoded-record JSONL files produced by "
                          "write_jsonl")
    msk.add_argument("--policy", required=True,
                     help="mask policy JSON: full field paths -> actions")
    key_src = msk.add_mutually_exclusive_group(required=True)
    key_src.add_argument("--key-env",
                         help="name of the environment variable holding "
                              "the mapping key")
    key_src.add_argument("--key-file",
                         help="file containing the mapping key")
    msk.add_argument("--out-dir", required=True,
                     help="new delivery directory (must not exist yet)")
    msk.add_argument("--chunk-size", type=int, default=5000,
                     help="lines processed per batch (does not affect "
                          "results; default 5000)")
    msk.add_argument("--temp-dir",
                     help="base directory for the SQLite staging area; "
                          "only the unique subdirectory created for this "
                          "run is cleaned up")

    args = parser.parse_args(argv)
    if args.command == "decode":
        return _run_decode(args)
    if args.command == "audit":
        return _run_audit(args)
    if args.command == "mask":
        return _run_mask(args)
    return _run_impact(args)


def _run_decode(args) -> int:
    try:
        manifest = Manifest.load(args.manifest)
        result = decode_file(args.file, sender=args.sender,
                             business_date=args.date, manifest=manifest,
                             copybook=args.copybook)
    except CopybookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out:
        result.write_jsonl(args.out)
    if args.quarantine_dir:
        result.export_quarantine(args.quarantine_dir)

    ref = result.layout_ref
    print(f"layout      : {ref.copybook} version {ref.version} "
          f"(sender={ref.sender})")
    print(f"lines       : {result.total_lines} total, "
          f"{result.ok_count} decoded, {result.quarantine_count} quarantined")
    if args.out:
        print(f"records     : {args.out}")
    if args.quarantine_dir:
        print(f"quarantine  : {args.quarantine_dir}")
    return 0


def _run_audit(args) -> int:
    try:
        config = AuditConfig.load(args.config)
        report = run_audit(
            args.records, args.quarantine_index, config=config,
            expected_source=args.expected_source,
            expected_lines=args.expected_lines,
            output_dir=args.out_dir, temp_dir=args.temp_dir)
    except (AuditConfigError, AuditInputError) as exc:
        print(f"error: cannot validate: {exc}", file=sys.stderr)
        return EXIT_CANNOT_VALIDATE
    except CopybookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CANNOT_VALIDATE

    print(f"conclusion  : {report['conclusion']}")
    print(f"batches     : {report['batch_count']} seen, "
          f"{report['rejected_batch_count']} rejected")
    print(f"accepted    : {report['accepted_detail_count']} "
          f"detail line(s) -> {args.out_dir}/accepted_details.jsonl")
    print(f"report      : {args.out_dir}/audit_report.json")
    report_path = Path(args.out_dir) / "audit_report.json"
    full_report = json.loads(report_path.read_text(encoding="utf-8"))
    for diag in full_report["file_diagnostics"][:20]:
        line = diag["line_number"]
        prefix = f"line {line}" if line is not None else "file"
        print(f"  [{prefix}] {diag['code']}: {diag['message']}")
    if full_report.get("file_diagnostic_count", 0) > 20:
        print(f"  ... {full_report['file_diagnostic_count'] - 20} more "
              "file diagnostics in audit_report.json")
    if report["conclusion"] == BUSINESS_REJECTED:
        return EXIT_BUSINESS_REJECT
    return EXIT_OK


def _run_mask(args) -> int:
    try:
        policy = MaskPolicy.load(args.policy)
        if args.key_env:
            key = MaskKey.from_env(args.key_env)
        else:
            key = MaskKey.from_file(args.key_file)
        summary = run_mask(
            args.inputs, policy=policy, key=key, output_dir=args.out_dir,
            chunk_size=args.chunk_size, temp_dir=args.temp_dir)
    except MaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MASK_FAILED
    except CopybookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MASK_FAILED

    print(f"conclusion  : {summary['conclusion']}")
    print(f"files       : {summary['file_count']} input file(s), "
          f"{summary['records_in']} record(s) in, "
          f"{summary['records_out']} delivered, "
          f"{summary['records_rejected']} rejected")
    print(f"groups      : {summary['group_count']} link group(s), "
          f"{summary['distinct_pseudonym_values']} distinct value(s) "
          "mapped")
    print(f"records     : {args.out_dir}/<input-stem>.masked.jsonl")
    print(f"report      : {args.out_dir}/{summary['report_json']}")
    print(f"per-record  : {args.out_dir}/{summary['record_report']}")
    return EXIT_MASK_OK


def _run_impact(args) -> int:
    try:
        summary = run_impact_report(args.old_manifest, args.new_manifest,
                                    args.out_dir)
    except ImpactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IMPACT_UNVERIFIABLE

    print(f"conclusion  : {summary['conclusion']}")
    print(f"problems    : {summary['problem_count']}")
    print(f"senders     : {summary['sender_count']} sender(s), "
          f"{summary['affected_range_count']} affected date range(s), "
          f"{summary['incompatible_comparison_count']} incompatible "
          "layout comparison(s)")
    print(f"report      : {args.out_dir}/{summary['report_file']}")
    print(f"field diff  : {args.out_dir}/{summary['diff_file']}")
    if summary["conclusion"] == COMPATIBLE:
        return EXIT_IMPACT_COMPATIBLE
    if summary["conclusion"] == INCOMPATIBLE:
        return EXIT_IMPACT_INCOMPATIBLE
    return EXIT_IMPACT_UNVERIFIABLE


if __name__ == "__main__":
    sys.exit(main())
