"""
amend CLI entry point.

Usage:
  amend diff --from 2021-01-01 --to 2026-09-01 [--substantive] [--json FILE]
  amend validate PATH [--from DATE --to DATE]
  amend graph [--repo REPO] [--out FILE]
  amend canary [--repo REPO] [--json FILE]
  amend guard [--repo REPO] [--json FILE]
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

    # graph subcommand
    graph_p = subparsers.add_parser("graph", help="Build code graph for a target repo")
    graph_p.add_argument("--repo", default="demo/lendwise", metavar="REPO",
                         help="Path to target repo (default: demo/lendwise)")
    graph_p.add_argument("--out", default=None, metavar="FILE",
                         help="Write graph JSON to FILE (e.g. .amend/graph.json)")
    graph_p.set_defaults(func=_cmd_graph)

    # canary subcommand
    canary_p = subparsers.add_parser("canary", help="Run dynamic canary sweep")
    canary_p.add_argument("--repo", default="demo/lendwise", metavar="REPO",
                          help="Path to target repo (default: demo/lendwise)")
    canary_p.add_argument("--json", default=None, metavar="FILE",
                          help="Write hits to JSON file")
    canary_p.set_defaults(func=_cmd_canary)

    # guard subcommand
    guard_p = subparsers.add_parser("guard", help="Run zero-LLM compliance gate")
    guard_p.add_argument("--repo", default="demo/lendwise", metavar="REPO",
                          help="Path to target repo (default: demo/lendwise)")
    guard_p.add_argument("--json", default=None, metavar="FILE",
                          help="Write failures to JSON file")
    guard_p.set_defaults(func=_cmd_guard)

    # verify subcommand
    verify_p = subparsers.add_parser("verify", help="Run fail-before/pass-after proof engine")
    verify_p.add_argument("--repo", default="demo/lendwise", metavar="REPO",
                          help="Path to target repo (default: demo/lendwise)")
    verify_p.add_argument("--base", default="HEAD~1", metavar="REF",
                          help="Base git ref (default: HEAD~1)")
    verify_p.set_defaults(func=cmd_verify)

    # evidence subcommand
    evidence_p = subparsers.add_parser("evidence", help="Build evidence pack")
    evidence_p.add_argument("--run-id", default=None, dest="run_id", metavar="ID",
                            help="Run ID (default: auto-generated)")
    evidence_p.add_argument("--repo", default="demo/lendwise", metavar="REPO",
                            help="Path to target repo (default: demo/lendwise)")
    evidence_p.add_argument("--base", default="HEAD~1", metavar="REF",
                            help="Base git ref (default: HEAD~1)")
    evidence_p.set_defaults(func=cmd_evidence)

    args = parser.parse_args()
    args.func(args)


def _cmd_graph(args: argparse.Namespace) -> None:
    from amend.graph import cmd_graph
    cmd_graph(args)


def _cmd_canary(args: argparse.Namespace) -> None:
    from amend.canary import cmd_canary
    cmd_canary(args)


def _cmd_guard(args: argparse.Namespace) -> None:
    from amend.guard import cmd_guard
    cmd_guard(args)


def _git_root(path: Path) -> Path:
    """Top level of the git repository that contains *path* (the app may be a subfolder)."""
    import subprocess
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                         capture_output=True, text=True)
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 else path


def _run_state(returncode: int) -> str:
    return "PASS" if returncode == 0 else "FAIL" if returncode == 1 else "ERROR"


def _rel(path: Path) -> str:
    """Path relative to the current directory when possible, never the home folder."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def cmd_verify(args: argparse.Namespace) -> None:
    from amend.verify import verify
    app_dir = Path(args.repo).resolve()
    repo = _git_root(app_dir)
    result = verify(repo, app_dir, base_ref=args.base)
    s = result["summary"]
    print("Fail before, pass after:")
    for t in result["obligation_tests"]:
        base = _run_state(t["base_returncode"])
        head = _run_state(t["head_returncode"])
        verdict = "discriminating" if t["discriminating"] else "REJECTED"
        print(f"  base {base:5s} head {head:5s} {verdict:14s} {t['test_file']}")
    print(f"\nVerification complete.")
    print(f"  Obligation tests : {s['total_obligation_tests']}")
    print(f"  Discriminating   : {s['discriminating']}")
    print(f"  Rejected         : {s['rejected']}")
    print(f"  Passing (head)   : {s['passing_head']}")
    print(f"  Integrity clean  : {s['integrity_clean']}")
    print(f"  Regression green : {s['regression_green']}")
    approved = result["integrity"].get("approved_changes", [])
    if approved:
        print(f"  Human-approved test rewrites: {len(approved)} (compliance/test_changes.yaml)")
    out = repo / ".amend" / "verification.json"
    print(f"\nWritten: {_rel(out)}")


def cmd_evidence(args: argparse.Namespace) -> None:
    from amend.evidence import build_evidence
    app_dir = Path(args.repo).resolve()
    repo = _git_root(app_dir)
    cert = build_evidence(repo, app_dir, base_ref=args.base, run_id=args.run_id)
    counts = cert["status_counts"]
    print("Evidence pack built.")
    for status, n in counts.items():
        print(f"  {status}: {n}")
    run_id = cert["meta"]["run_id"]
    ev_dir = repo / "evidence" / run_id
    print(f"\nEvidence written to: {_rel(ev_dir)}")


if __name__ == "__main__":
    main()
