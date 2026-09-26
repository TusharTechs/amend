"""
amend CLI entry point.

Usage:
  amend diff --from 2021-01-01 --to 2026-09-01 [--substantive] [--json FILE]
  amend validate PATH [--from DATE --to DATE]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from amend.regdiff import parse, diff as _diff
from amend.obligations import load, validate


REG_DIR = Path(__file__).parent.parent / "regulations" / "16cfr314"


def _version_path(date: str) -> Path:
    return REG_DIR / f"{date}.xml"


def cmd_diff(args: argparse.Namespace) -> None:
    from_date: str = args.from_date
    to_date: str = args.to_date

    old_path = _version_path(from_date)
    new_path = _version_path(to_date)

    if not old_path.exists():
        print(f"error: no regulation file for date {from_date}", file=sys.stderr)
        sys.exit(1)
    if not new_path.exists():
        print(f"error: no regulation file for date {to_date}", file=sys.stderr)
        sys.exit(1)

    old_v = parse(old_path, from_date)
    new_v = parse(new_path, to_date)
    entries = _diff(old_v, new_v)

    counts: dict[str, int] = {}
    for e in entries:
        counts[e.change] = counts.get(e.change, 0) + 1

    substantive_types = {"added", "removed", "modified"}
    substantive = [e for e in entries if e.change in substantive_types]

    print(f"Diff {from_date} → {to_date}")
    for change_type in ("added", "removed", "modified", "renumbered", "unchanged"):
        n = counts.get(change_type, 0)
        if n:
            print(f"  {change_type:12s}: {n}")

    if args.substantive or True:  # always print substantive citations
        print("\nSubstantive changes:")
        for e in substantive:
            print(f"  [{e.change}] {e.citation}")

    if args.json:
        out = []
        for e in entries:
            out.append({
                "citation": e.citation,
                "change": e.change,
                "old_text": e.old_text,
                "new_text": e.new_text,
                "similarity": e.similarity,
                "ops": e.ops,
                "old_citation": e.old_citation,
            })
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nFull diff written to {args.json}")


def cmd_validate(args: argparse.Namespace) -> None:
    from_date: str | None = args.from_date
    to_date: str | None = args.to_date

    obligations = load(args.path)
    if not obligations:
        print(f"No obligations found in {args.path}", file=sys.stderr)
        sys.exit(1)

    # Determine which versions to load
    dates_needed: set[str] = set()
    for ob in obligations:
        if ob.version:
            dates_needed.add(ob.version)
    if from_date:
        dates_needed.add(from_date)
    if to_date:
        dates_needed.add(to_date)

    versions: dict[str, object] = {}
    for date in dates_needed:
        p = _version_path(date)
        if p.exists():
            from amend.regdiff import Version
            versions[date] = parse(p, date)

    # Build redline if both dates given
    redline = []
    if from_date and to_date:
        old_v = versions.get(from_date)
        new_v = versions.get(to_date)
        if old_v and new_v:
            redline = _diff(old_v, new_v)

    problems = validate(obligations, versions, redline)
    if problems:
        for p in problems:
            print(p)
        sys.exit(1)
    else:
        print("All obligations valid.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="amend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # diff subcommand
    diff_p = subparsers.add_parser("diff", help="Diff two regulation versions")
    diff_p.add_argument("--from", dest="from_date", required=True, metavar="DATE")
    diff_p.add_argument("--to", dest="to_date", required=True, metavar="DATE")
    diff_p.add_argument("--substantive", action="store_true",
                        help="Only show substantive changes")
    diff_p.add_argument("--json", metavar="FILE",
                        help="Write full diff to JSON file")
    diff_p.set_defaults(func=cmd_diff)

    # validate subcommand
    val_p = subparsers.add_parser("validate", help="Validate obligation YAML")
    val_p.add_argument("path", help="YAML file or folder")
    val_p.add_argument("--from", dest="from_date", default=None, metavar="DATE")
    val_p.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    val_p.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
