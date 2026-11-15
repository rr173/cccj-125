"""Command line interface: decode a data file against a manifest."""

from __future__ import annotations

import argparse
import sys

from .errors import CopybookError
from .manifest import Manifest
from .pipeline import decode_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="copybook",
        description="Decode fixed-width EBCDIC data files using versioned "
                    "copybook layouts.")
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
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
