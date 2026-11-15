"""Command line interface: decode data files, audit batch handoffs,
report the impact of layout changes between two receiving manifests,
generate de-identified samples from decoded records, replay open-item
settlement, and read offline Git handoff bundles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import (AuditConfig, AuditConfigError, AuditInputError,
                    BUSINESS_REJECTED, run_audit)
from .errors import CopybookError
from .gitkit import (EXIT_UNUSABLE as EXIT_GIT_UNUSABLE,
                     GitHandoffError, export_blob, ingest_handoff,
                     inspect_handoff)
from .impact import (COMPATIBLE, INCOMPATIBLE, ImpactError,
                     run_impact_report)
from .manifest import Manifest
from .mask import MaskError, MaskKey, MaskPolicy, run_mask
from .pipeline import decode_file
from .relation import (EXIT_BUSINESS_REJECT as EXIT_ADMIT_REJECT,
                      EXIT_CANNOT_PROCESS as EXIT_ADMIT_FAILED)
from .relation import (RelationConfigError, RelationInputError,
                      RelationQueryError, RelationContract)
from .relation import explain_row, get_proof_node, iter_kept_rows, run_admission
from .settle import (EXIT_BUSINESS_REJECT as EXIT_SETTLE_REJECT,
                     EXIT_CANNOT_PROCESS as EXIT_SETTLE_FAILED)
from .settle import (SettleConfig, SettleConfigError, SettleInputError,
                     SettleQueryError)
from .settle import run_settlement, trace_object

# Exit codes for the audit subcommand.
EXIT_OK = 0                 # structure valid, every batch accepted
EXIT_BUSINESS_REJECT = 1   # audited; at least one batch/file is refused
EXIT_CANNOT_VALIDATE = 2   # input evidence or configuration is unusable

# Exit codes for the impact subcommand.
EXIT_IMPACT_COMPATIBLE = 0     # every affected date range stays compatible
EXIT_IMPACT_INCOMPATIBLE = 1   # report written; breaking changes found
EXIT_IMPACT_UNVERIFIABLE = 2   # problems prevent a complete comparison

# Exit codes for the git subcommands.
EXIT_GIT_OK = 0                # bundle verified and facts delivered


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

    stl = sub.add_parser(
        "settle",
        help="replay bill/receipt/cancel events into open-item allocations "
             "as of a cutoff, emitting balances, aging and the full "
             "allocation history")
    stl.add_argument("inputs", nargs="+",
                     help="decoded event JSONL files (meta/fields wrapper)")
    stl.add_argument("--config", required=True,
                     help="settlement config JSON: paths for event_id, "
                          "account, currency, at, kind, object_id, amount, "
                          "due_on, target_id")
    stl.add_argument("--cutoff", required=True,
                     help="ISO 8601 offset timestamp; only events at <= "
                          "cutoff (UTC) are applied")
    stl.add_argument("--out-dir", required=True,
                     help="new result directory (must not exist yet)")

    trc = sub.add_parser(
        "trace",
        help="trace one bill or receipt through a delivered settle result "
             "without any original input files")
    trc.add_argument("result_dir",
                     help="settle output directory produced by 'settle'")
    trc.add_argument("object_id",
                     help="the bill or receipt object_id to trace")
    trc.add_argument("--kind", choices=["BILL", "RECEIPT"],
                     help="disambiguate when the id exists in both "
                          "namespaces")

    adm = sub.add_parser(
        "admit",
        help="relational admission: keep the greatest set of records "
             "mutually supported by the reference contract, with checkable "
             "rejection proofs")
    adm.add_argument("--contract", required=True,
                     help="reference contract JSON: tables, files, "
                          "row_id_path, fields, candidate_keys, references")
    adm.add_argument("--out-dir", required=True,
                     help="new result directory (must not exist yet)")
    adm.add_argument("--temp-dir", required=True,
                     help="base directory for SQLite disk staging; only the "
                          "unique subdirectory created for this run is "
                          "cleaned up")

    aex = sub.add_parser(
        "admit-explain",
        help="explain one row's kept witnesses or rejection proof from a "
             "delivered admission result, without any original input files")
    aex.add_argument("result_dir",
                     help="admission output directory produced by 'admit'")
    aex.add_argument("table", help="logical table name")
    aex.add_argument("row_id", help="the row_id to explain")
    aex.add_argument("--proof-id", type=int, default=None,
                     help="print one shared proof node by id instead of "
                          "the row explanation")

    atb = sub.add_parser(
        "admit-table",
        help="stream the admitted rows of one table from a delivered "
             "admission result in row_id UTF-8 byte order")
    atb.add_argument("result_dir",
                     help="admission output directory produced by 'admit'")
    atb.add_argument("table", help="logical table name")

    git = sub.add_parser(
        "git-ingest",
        help="verify a self-contained Git bundle v2 offline and write its "
             "commit graph, per-commit file listings and per-parent diffs "
             "(no git/libgit2, no checkout)")
    git.add_argument("bundle", help="path to a self-contained v2 bundle")
    git.add_argument("--out-dir", required=True,
                     help="new fact directory (must not exist yet)")
    git.add_argument("--temp-dir", required=True,
                     help="base directory for the object staging area; only "
                          "the unique subdirectory created for this run is "
                          "cleaned up")
    git.add_argument("--commit", action="append", default=None,
                     metavar="ID",
                     help="restrict listings/diffs to this commit "
                          "(repeatable; default: every reachable commit)")
    _add_git_limits(git)

    gx = sub.add_parser(
        "git-export",
        help="extract one regular/executable blob at an exact path in one "
             "commit as a new file, with an export manifest")
    gx.add_argument("bundle", help="path to a self-contained v2 bundle")
    gx.add_argument("commit_id", help="40-hex commit id reachable from a ref")
    gx.add_argument("--path", required=True,
                    help="lossless path: each raw name component "
                         "base64-encoded, joined by '/' (e.g. YWxwaGEudHh0 "
                         "for 'alpha.txt')")
    gx.add_argument("--out", required=True,
                    help="new destination file (must not exist)")
    gx.add_argument("--temp-dir", required=True,
                    help="base directory for the object staging area")
    _add_git_limits(gx)

    gq = sub.add_parser(
        "git-query",
        help="answer commit/path queries against a bundle without writing "
             "an output directory")
    gq.add_argument("bundle", help="path to a self-contained v2 bundle")
    gq.add_argument("--temp-dir", required=True,
                    help="base directory for the object staging area")
    gq.add_argument("--commit",
                    help="commit id for --files/--diff (default: a ref's "
                         "tip when --log is not requested)")
    gq.add_argument("--files", action="store_true",
                    help="print the complete file listing of --commit")
    gq.add_argument("--diff", action="store_true",
                    help="print the change list of --commit against each "
                         "parent")
    gq.add_argument("--log", action="store_true",
                    help="print the commit graph in parents-first order")
    _add_git_limits(gq)

    args = parser.parse_args(argv)
    if args.command == "decode":
        return _run_decode(args)
    if args.command == "audit":
        return _run_audit(args)
    if args.command == "mask":
        return _run_mask(args)
    if args.command == "settle":
        return _run_settle(args)
    if args.command == "trace":
        return _run_trace(args)
    if args.command == "admit":
        return _run_admit(args)
    if args.command == "admit-explain":
        return _run_admit_explain(args)
    if args.command == "admit-table":
        return _run_admit_table(args)
    if args.command == "git-ingest":
        return _run_git_ingest(args)
    if args.command == "git-export":
        return _run_git_export(args)
    if args.command == "git-query":
        return _run_git_query(args)
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


def _run_settle(args) -> int:
    try:
        config = SettleConfig.load(args.config)
        summary = run_settlement(
            args.inputs, config=config, cutoff=args.cutoff,
            output_dir=args.out_dir)
    except SettleConfigError as exc:
        print(f"error: invalid settlement config: {exc}", file=sys.stderr)
        return EXIT_SETTLE_FAILED
    except SettleInputError as exc:
        print(f"error: cannot process settlement input: {exc}",
              file=sys.stderr)
        return EXIT_SETTLE_FAILED

    print(f"conclusion  : {summary['conclusion']}")
    print(f"events      : {summary['event_count']} read, "
          f"{summary['applied_event_count']} applied, "
          f"{summary['post_cutoff_event_count']} after cutoff")
    print(f"groups      : {summary['group_count']} group(s), "
          f"{summary['valid_group_count']} valid, "
          f"{summary['invalid_group_count']} invalid")
    print(f"cutoff      : {summary['cutoff']['utc']} "
          f"(base day {summary['cutoff']['base_day']})")
    print(f"result      : {args.out_dir}/settle_report.json")
    if summary["invalid_group_count"]:
        return EXIT_SETTLE_REJECT
    return EXIT_OK


def _run_trace(args) -> int:
    try:
        result = trace_object(args.result_dir, args.object_id,
                              kind=args.kind)
    except (SettleQueryError, SettleInputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SETTLE_FAILED
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def _run_admit(args) -> int:
    try:
        contract = RelationContract.load(args.contract)
        summary = run_admission(contract=contract,
                                output_dir=args.out_dir,
                                temp_dir=args.temp_dir)
    except RelationConfigError as exc:
        print(f"error: invalid reference contract: {exc}", file=sys.stderr)
        return EXIT_ADMIT_FAILED
    except RelationInputError as exc:
        print(f"error: cannot process admission input: {exc}",
              file=sys.stderr)
        return EXIT_ADMIT_FAILED
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADMIT_FAILED

    print(f"conclusion  : {summary['conclusion']}")
    print(f"identities  : {summary['identity_count']} input, "
          f"{summary['proof_node_count']} proof node(s), "
          f"{summary['proof_edge_count']} proof edge(s), "
          f"{summary['witness_count']} witness(es)")
    for name, counts in summary["counts"].items():
        print(f"table {name}: {counts['input']} in / {counts['kept']} kept "
              f"/ {counts['local_reject']} local rejected / "
              f"{counts['cascade_reject']} cascade rejected")
    print(f"result      : {args.out_dir}/admission_report.json")
    if summary["conclusion"] == "business_rejected":
        return EXIT_ADMIT_REJECT
    return EXIT_OK


def _run_admit_explain(args) -> int:
    try:
        if args.proof_id is not None:
            payload = get_proof_node(args.result_dir, args.proof_id)
        else:
            payload = explain_row(args.result_dir, args.table, args.row_id)
    except (RelationQueryError, RelationInputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADMIT_FAILED
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def _run_admit_table(args) -> int:
    try:
        rows = iter_kept_rows(args.result_dir, args.table)
        count = 0
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
            count += 1
    except (RelationQueryError, RelationInputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADMIT_FAILED
    print(f"# {count} admitted row(s) from table {args.table}",
          file=sys.stderr)
    return EXIT_OK


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


def _add_git_limits(parser) -> None:
    parser.add_argument("--max-object-bytes", type=int, default=None,
                        help="hard limit on one inflated object "
                             "(default 67108864 = 64 MiB)")
    parser.add_argument("--max-total-bytes", type=int, default=None,
                        help="hard limit on cumulative inflated/staged "
                             "bytes (default 1073741824 = 1 GiB)")
    parser.add_argument("--max-objects", type=int, default=None,
                        help="hard limit on pack object count "
                             "(default 1000000)")
    parser.add_argument("--max-delta-depth", type=int, default=None,
                        help="hard limit on delta chain depth (default 64)")


def _git_limit_kwargs(args) -> dict:
    return {
        "max_object_bytes": args.max_object_bytes,
        "max_total_bytes": args.max_total_bytes,
        "max_objects": args.max_objects,
        "max_delta_depth": args.max_delta_depth,
    }


def _run_git_ingest(args) -> int:
    try:
        summary = ingest_handoff(
            args.bundle, output_dir=args.out_dir, temp_dir=args.temp_dir,
            commit_ids=args.commit, **_git_limit_kwargs(args))
    except GitHandoffError as exc:
        print(f"error: cannot ingest bundle: {exc}", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    except OSError as exc:
        print(f"error: cannot ingest bundle: {exc}", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    print(f"conclusion  : {summary['conclusion']}")
    print(f"bundle sha1 : {summary['bundle_sha1']}")
    print(f"refs        : {summary['ref_count']}")
    print(f"commits     : {summary['commit_count']} reachable, "
          f"{summary['listed_commit_count']} listed")
    print(f"objects     : {summary['object_count']}")
    print(f"report      : {summary['report']}")
    print(f"commits     : {summary['commits']}")
    print(f"files/diffs : {Path(args.out_dir) / 'commits'}/")
    return EXIT_GIT_OK


def _run_git_export(args) -> int:
    try:
        manifest = export_blob(
            args.bundle, commit_id=args.commit_id, path=args.path,
            destination=args.out, temp_dir=args.temp_dir,
            **_git_limit_kwargs(args))
    except GitHandoffError as exc:
        print(f"error: cannot export: {exc}", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    except OSError as exc:
        print(f"error: cannot export: {exc}", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    print(f"conclusion  : {manifest['conclusion']}")
    print(f"path        : {manifest['path']['display']} "
          f"(b64 {manifest['path']['b64']})")
    print(f"commit      : {manifest['commit']}")
    print(f"blob        : {manifest['blob']['object_id']} "
          f"({manifest['blob']['size']} bytes)")
    print(f"mode        : {manifest['mode']} ({manifest['kind']})")
    print(f"written     : {args.out}")
    print(f"manifest    : {args.out}.export_manifest.json")
    return EXIT_GIT_OK


def _run_git_query(args) -> int:
    if not (args.log or args.files or args.diff):
        print("error: choose at least one of --log, --files, --diff",
              file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    if (args.files or args.diff) and not args.commit:
        print("error: --files/--diff need --commit", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    selected = None if args.log else (
        [args.commit] if args.commit else None)
    try:
        report = inspect_handoff(
            args.bundle, temp_dir=args.temp_dir,
            commit_ids=selected, include_diffs=args.diff,
            **_git_limit_kwargs(args))
    except GitHandoffError as exc:
        print(f"error: cannot query bundle: {exc}", file=sys.stderr)
        return EXIT_GIT_UNUSABLE
    if args.log:
        for commit in report["commits"]:
            parents = " ".join(commit["parents"]) or "-"
            refs = ",".join(commit["refs"])
            print(f"{commit['object_id']} parents={parents} refs={refs}")
    if args.files:
        listing = report["file_listings"][args.commit]
        for fact in listing["files"]:
            print(json.dumps(fact, ensure_ascii=False))
    if args.diff:
        for diff in report["diffs"][args.commit]:
            row = {"parent_index": diff["parent_index"],
                   "parent": diff["parent"],
                   "change_count": diff["change_count"],
                   "changes": diff["changes"]}
            print(json.dumps(row, ensure_ascii=False))
    return EXIT_GIT_OK


if __name__ == "__main__":
    sys.exit(main())
