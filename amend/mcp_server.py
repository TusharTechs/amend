"""amend/mcp_server.py — Amend stdio MCP server (FastMCP).

Run via:  python -m amend.mcp_server
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver import MCPServer as FastMCP

# ---------------------------------------------------------------------------
# Locate repo root and app dir from compliance/amend.yaml
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path:
    """Walk up from cwd looking for compliance/amend.yaml."""
    p = Path.cwd()
    for candidate in [p, *p.parents]:
        if (candidate / "compliance" / "amend.yaml").exists():
            return candidate
    # Fallback: assume cwd is the repo root
    return p


def _load_cfg(repo_root: Path) -> dict:
    cfg_path = repo_root / "compliance" / "amend.yaml"
    if cfg_path.exists():
        with open(cfg_path) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _app_dir(repo_root: Path, cfg: dict) -> Path:
    """Return the app directory (workspace). Default: demo/lendwise."""
    app_rel = cfg.get("app_dir", "demo/lendwise")
    return (repo_root / app_rel).resolve()


def _events_path(repo_root: Path) -> Path:
    d = repo_root / ".amend"
    d.mkdir(exist_ok=True)
    return d / "events.ndjson"


def _append_event(repo_root: Path, tool: str, summary: str) -> None:
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "summary": summary,
    }
    with open(_events_path(repo_root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


def _truncate(lst: list, n: int = 50) -> list:
    return lst[:n]


# ---------------------------------------------------------------------------
# Resolve globals lazily (at first call, not import time)
# ---------------------------------------------------------------------------

_repo_root: Path | None = None
_app: Path | None = None
_cfg: dict | None = None


def _ctx() -> tuple[Path, Path, dict]:
    global _repo_root, _app, _cfg
    if _repo_root is None:
        _repo_root = _find_repo_root()
        _cfg = _load_cfg(_repo_root)
        _app = _app_dir(_repo_root, _cfg)
    return _repo_root, _app, _cfg  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("amend")


@mcp.tool()
def get_redline(from_date: str, to_date: str, substantive_only: bool = True) -> dict:
    """Return regulation redline (added/removed/modified clauses) between two dates."""
    from amend.regdiff import parse, diff as reg_diff

    repo_root, app, cfg = _ctx()
    reg_folder = cfg.get("regulations_folder", "regulations/16cfr314")
    reg_dir = (repo_root / reg_folder).resolve()

    old_v = parse(reg_dir / f"{from_date}.xml", from_date)
    new_v = parse(reg_dir / f"{to_date}.xml", to_date)
    entries = reg_diff(old_v, new_v)

    if substantive_only:
        entries = [e for e in entries if e.change != "unchanged"]

    result_entries = [
        {
            "citation": e.citation,
            "change": e.change,
            "text": (e.new_text or e.old_text or "")[:200],
        }
        for e in _truncate(entries)
    ]

    result = {"count": len(entries), "entries": result_entries}
    _append_event(repo_root, "get_redline", f"{len(entries)} entries {from_date}->{to_date}")
    return result


@mcp.tool()
def validate_obligations() -> dict:
    """Validate all obligation YAML files in compliance/obligations/ against the redline."""
    from amend.obligations import load, validate
    from amend.regdiff import parse

    repo_root, app, cfg = _ctx()
    ob_dir = app / "compliance" / "obligations"
    obligations = load(ob_dir) if ob_dir.exists() else []

    reg_folder = cfg.get("regulations_folder", "regulations/16cfr314")
    reg_dir = (repo_root / reg_folder).resolve()
    from_date = cfg.get("from_date", "2021-01-01")
    to_date = cfg.get("to_date", "2026-09-01")

    from amend.regdiff import diff as reg_diff
    old_v = parse(reg_dir / f"{from_date}.xml", from_date)
    new_v = parse(reg_dir / f"{to_date}.xml", to_date)
    redline = reg_diff(old_v, new_v)

    versions = {from_date: old_v, to_date: new_v}
    also_date = cfg.get("also_date")
    if also_date:
        versions[also_date] = parse(reg_dir / f"{also_date}.xml", also_date)

    problems = validate(obligations, versions, redline)
    result = {
        "obligation_count": len(obligations),
        "problem_count": len(problems),
        "problems": _truncate([str(p) for p in problems]),
    }
    _append_event(repo_root, "validate_obligations", f"{len(problems)} problems")
    return result


@mcp.tool()
def approve_obligation(obligation_id: str, approver: str) -> dict:
    """Set status=approved and approved_by on an obligation YAML, preserving all other fields."""
    repo_root, app, cfg = _ctx()
    ob_dir = app / "compliance" / "obligations"
    target: Path | None = None
    for f in sorted(ob_dir.glob("*.yaml")):
        raw = f.read_text(encoding="utf-8")
        if f"id: {obligation_id}" in raw or f"id: '{obligation_id}'" in raw:
            target = f
            break

    if target is None:
        return {"ok": False, "error": f"obligation {obligation_id!r} not found"}

    with open(target) as fh:
        data = yaml.safe_load(fh)

    # Handle list or single-record YAML
    if isinstance(data, list):
        for item in data:
            if str(item.get("id", "")) == obligation_id:
                item["status"] = "approved"
                item["approved_by"] = approver
    elif isinstance(data, dict):
        if str(data.get("id", "")) == obligation_id:
            data["status"] = "approved"
            data["approved_by"] = approver

    with open(target, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)

    _append_event(repo_root, "approve_obligation", f"{obligation_id} approved by {approver}")
    return {"ok": True, "obligation_id": obligation_id, "approved_by": approver}


@mcp.tool()
def query_graph(query: str, arg: str) -> dict:
    """Query the code graph. query: routes_missing_dependency | sinks_of | trace."""
    from amend.graph import build as build_graph

    repo_root, app, cfg = _ctx()
    g = build_graph(app, cfg)

    if query == "routes_missing_dependency":
        nodes = g.routes_missing_dependency(arg)
        result = [{"id": n.id, "file": n.file, "line": n.line} for n in _truncate(nodes)]
    elif query == "sinks_of":
        nodes = g.sinks_of(arg)
        result = [{"id": n.id, "file": n.file, "line": n.line} for n in _truncate(nodes)]
    elif query == "trace":
        paths = g.trace(arg)
        result = _truncate(paths)
    else:
        _append_event(repo_root, "query_graph", f"unknown query {query!r}")
        return {"error": f"unknown query {query!r}; use routes_missing_dependency|sinks_of|trace"}

    _append_event(repo_root, "query_graph", f"{query}({arg!r}) -> {len(result)} items")
    return {"query": query, "arg": arg, "count": len(result), "result": result}


@mcp.tool()
def check_fails_on_base(test_file: str) -> dict:
    """Return the verify() entry for a specific compliance test file."""
    from amend.verify import verify

    repo_root, app, cfg = _ctx()
    base_ref = cfg.get("base_ref", "main")
    result = verify(repo_root, app, base_ref=base_ref)

    matches = [
        r for r in result.get("obligation_tests", [])
        if r["test_file"] == test_file or r["test_file"] == Path(test_file).name
    ]
    entry = matches[0] if matches else {"error": "test file not found in verify output"}
    _append_event(repo_root, "check_fails_on_base", f"{test_file}: {entry.get('discriminating','?')}")
    return entry


@mcp.tool()
def run_canary() -> dict:
    """Run the canary sweep and return all plaintext hits."""
    from amend.canary import run_canary as _run_canary

    repo_root, app, cfg = _ctx()
    hits = _run_canary(app) or []
    result = {"hit_count": len(hits), "hits": _truncate(hits)}
    _append_event(repo_root, "run_canary", f"{len(hits)} hits")
    return result


@mcp.tool()
def guard_check() -> dict:
    """Run all guard rules and return findings."""
    from amend.guard import run_guard

    repo_root, app, cfg = _ctx()
    failures = run_guard(app, cfg)
    findings = _truncate([f.to_dict() for f in failures])
    result = {"finding_count": len(failures), "findings": findings}
    _append_event(repo_root, "guard_check", f"{len(failures)} findings")
    return result


@mcp.tool()
def record_finding(
    obligation_id: str,
    file: str,
    line: int,
    message: str,
    source: str,
) -> dict:
    """Append an open finding to compliance/findings.json."""
    repo_root, app, cfg = _ctx()
    findings_path = app / "compliance" / "findings.json"
    existing: list[dict] = []
    if findings_path.exists():
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            existing = data

    entry: dict[str, Any] = {
        "obligation_id": obligation_id,
        "file": file,
        "line": line,
        "message": message,
        "source": source,
        "status": "open",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    existing.append(entry)
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    _append_event(repo_root, "record_finding", f"{obligation_id} {file}:{line}")
    return {"ok": True, "entry": entry}


@mcp.tool()
def build_evidence(run_id: str = "") -> dict:
    """Build the evidence pack and return the certificate summary."""
    from amend.evidence import build_evidence as _build_evidence

    repo_root, app, cfg = _ctx()
    base_ref = cfg.get("base_ref", "main")
    cert = _build_evidence(repo_root, app, base_ref=base_ref, run_id=run_id or None)
    summary = {
        "run_id": cert["meta"]["run_id"],
        "status_counts": cert["status_counts"],
        "guard_findings": len(cert.get("guard_findings", [])),
        "canary_after_hits": len(cert.get("canary", {}).get("after", [])),
    }
    _append_event(repo_root, "build_evidence", f"run_id={summary['run_id']}")
    return summary


if __name__ == "__main__":
    mcp.run(transport="stdio")
