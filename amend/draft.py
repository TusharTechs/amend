"""amend/draft.py — draft obligations with IBM Granite, grounded in the clause text.

For each substantive clause in a redline, Granite on watsonx.ai reads the exact
eCFR text of that clause (never its memory of the rule) and proposes
obligations. Every draft then goes through the same validator as a human's
obligation: the quote must be a verbatim span of the clause, the citation must
exist and the fields must be well formed. Drafts that pass are written with
status "proposed"; a listed approver still has to approve each one.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from amend.obligations import Obligation, validate
from amend.regdiff import Version, diff
from amend.watsonx import WatsonxClient, WatsonxError, client_for

SYSTEM = (
    "You turn one clause of a US federal regulation into obligations an engineering team can implement and test. "
    "You are given the clause citation and its exact text. Use only that text, never your memory of the regulation. "
    "For each distinct requirement, copy the smallest exact span of the clause text that states it into \"quote\" "
    "(character for character, no paraphrase, no ellipsis), explain what the software or the team must do in "
    "\"statement\" (one or two plain sentences), and set \"shape\" to code, process or contract. "
    'Reply with JSON only: {"obligations": [{"quote": "...", "statement": "...", "shape": "code"}]}. '
    "Return an empty list if the clause states no requirement (for example a heading or a definition)."
)
MIN_CLAUSE_CHARS = 40


def _slug(citation: str) -> str:
    return re.sub(r"[^0-9a-z]", "", citation.lower())


def draft(old: Version, new: Version, client: WatsonxClient, limit: int | None = None) -> list[dict]:
    """Draft and validate obligations for every substantive added or modified clause."""
    redline = diff(old, new)
    targets = [e for e in redline
               if e.change in ("added", "modified") and len((e.new_text or "").strip()) >= MIN_CLAUSE_CHARS]
    if limit:
        targets = targets[:limit]

    def one(entry):
        reply = client.chat_json(SYSTEM, json.dumps({"citation": entry.citation, "text": entry.new_text}),
                                 list_key="obligations")
        return entry, reply.get("obligations", []) or []

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(one, targets))

    results = []
    for entry, proposed in replies:
        obligations = [
            Obligation(id=f"GR-{_slug(entry.citation)}-{n}", citation=entry.citation, version=new.date,
                       change=entry.change, quote=str(p.get("quote", "")), statement=str(p.get("statement", "")),
                       shape=str(p.get("shape", "")), status="proposed")
            for n, p in enumerate(proposed, 1)
        ]
        problems = validate(obligations, {new.date: new}, redline)
        by_id: dict[str, list[str]] = {}
        for pr in problems:
            by_id.setdefault(pr.obligation_id, []).append(str(pr))
        results.append({
            "citation": entry.citation,
            "change": entry.change,
            "accepted": [ob for ob in obligations if ob.id not in by_id],
            "rejected": [{"obligation": ob, "problems": by_id[ob.id]} for ob in obligations if ob.id in by_id],
        })
    return results


def write_drafts(results: list[dict], out_path: Path, model: str) -> int:
    accepted = [ob for r in results for ob in r["accepted"]]
    records = [{"id": ob.id, "citation": ob.citation, "version": ob.version, "change": ob.change,
                "quote": ob.quote, "statement": ob.statement, "shape": ob.shape, "status": ob.status,
                "exceptions": []} for ob in accepted]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# Drafted by {model} on IBM watsonx.ai from the verbatim clause text, then checked by\n"
              "# `amend validate`. Status is proposed: nothing here is approved or in force.\n")
    out_path.write_text(header + yaml.safe_dump(records, sort_keys=False, allow_unicode=True, width=100),
                        encoding="utf-8")
    return len(records)


def cmd_draft(args) -> None:
    from amend.cli import _git_root, _version_path
    from amend.regdiff import parse

    app_dir = Path(args.repo).resolve()
    old_p, new_p = _version_path(args.from_date), _version_path(args.to_date)
    for p, d in ((old_p, args.from_date), (new_p, args.to_date)):
        if not p.exists():
            print(f"error: no regulation file for date {d}", file=sys.stderr)
            sys.exit(1)
    try:
        client = client_for(_git_root(app_dir))
        results = draft(parse(old_p, args.from_date), parse(new_p, args.to_date), client, args.limit)
    except WatsonxError as exc:
        print(f"draft: {exc}", file=sys.stderr)
        sys.exit(2)
    out = app_dir / "compliance" / "drafts" / f"granite-{args.to_date}.yaml"
    n = write_drafts(results, out, client.config.model)
    total_rej = sum(len(r["rejected"]) for r in results)
    print(f"watsonx.ai · {client.config.model} · grounded on the clause text of {args.to_date}")
    for r in results:
        n_acc, n_rej = len(r["accepted"]), len(r["rejected"])
        print(f"  {r['citation']:18s} {n_acc + n_rej} drafted   {n_acc} verbatim   {n_rej} rejected")
        for rej in r["rejected"]:
            print(f"      rejected {rej['obligation'].id}: {rej['problems'][0].split(': ', 1)[-1]}")
    try:
        shown = out.relative_to(Path.cwd())
    except ValueError:
        shown = out
    print(f"\nDrafts: {n} proposed, {total_rej} rejected -> {shown}")
    print("Nothing is approved: each draft still needs approve_obligation from a listed approver.")
