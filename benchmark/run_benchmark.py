"""Score detection approaches against the seeded Lendwise ground truth.

Conditions (all zero-Bobcoin, all run on the seeded app on main):
  keyword  - regex search for obvious terms, the "grep the codebase" baseline
  semgrep  - Semgrep with the p/python and p/security-audit rulesets
  amend    - Amend's deterministic stages: guard (code graph) + canary sweep

A finding matches a ground-truth item when it points at the item's file, or at
one of the item's `also` locations (optionally requiring `contains` text on the
finding's line). The same rule applies to every condition. Decoys with an
`anchor` only count as flagged when the finding's source line contains it.

Usage:  python benchmark/run_benchmark.py   (from the repository root)
Writes: benchmark/results/summary.json and benchmark/results/summary.md
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "demo" / "lendwise"
RESULTS = ROOT / "benchmark" / "results"
KEYWORDS = re.compile(r"ssn|social|account|password|token|secret|mfa|otp|encrypt", re.IGNORECASE)
SKIP_DIRS = {"var", ".pytest_cache", "__pycache__", ".bob", "compliance"}


def _rel(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = (ROOT / p)
    try:
        return p.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _line_text(file: str, line: int) -> str:
    try:
        return (ROOT / file).read_text(errors="replace").splitlines()[line - 1]
    except (OSError, IndexError):
        return ""


# ── conditions ────────────────────────────────────────────────────────────────

def keyword_findings() -> list[dict]:
    out = []
    for p in sorted(APP.rglob("*")):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(APP).parts):
            continue
        if p.suffix not in {".py", ".sql", ".tf", ".env", ".json", ".sh"}:
            continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if KEYWORDS.search(line):
                out.append({"file": _rel(p), "line": n, "text": line.strip()[:160]})
    return out


def semgrep_findings() -> list[dict] | None:
    exe = shutil.which("semgrep") or str(Path(sys.executable).with_name("semgrep"))
    if not Path(exe).exists():
        return None
    proc = subprocess.run(
        [exe, "--config", "p/python", "--config", "p/security-audit", "--json", "--quiet",
         "--exclude", "var", "--exclude", "compliance", str(APP)],
        capture_output=True, text=True, cwd=ROOT,
    )
    try:
        results = json.loads(proc.stdout).get("results", [])
    except json.JSONDecodeError:
        return None
    return [{"file": _rel(r["path"]), "line": r["start"]["line"], "text": r["check_id"]} for r in results]


def amend_findings() -> list[dict]:
    from amend.canary import run_canary
    from amend.guard import run_guard

    out = [{"file": _rel(APP / f.file), "line": f.line, "text": f"{f.rule} {f.clause} {f.message}"}
           for f in run_guard(APP)]
    # Run the canary on a scratch copy so the benchmark never touches the demo's var/.
    tmp = Path(tempfile.mkdtemp(prefix="amend_bench_"))
    try:
        copy = tmp / "lendwise"
        shutil.copytree(APP, copy, ignore=shutil.ignore_patterns("var", "__pycache__", ".pytest_cache"))
        for h in run_canary(copy):
            rel = Path(h["path"]).resolve().relative_to(copy.resolve()).as_posix()
            out.append({"file": f"demo/lendwise/{rel}", "line": 0,
                        "text": f"canary {h['store_type']} {h['value_found']}"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ── scoring ───────────────────────────────────────────────────────────────────

def _matches(finding: dict, item: dict) -> bool:
    places = [{"file": item["file"]}] + item.get("also", [])
    for place in places:
        f = place["file"]
        hit = finding["file"] == f or (f.endswith("/") and finding["file"].startswith(f))
        if not hit:
            continue
        need = place.get("contains")
        if need is None:
            return True
        line = finding.get("text", "") + " " + (_line_text(finding["file"], finding["line"]) if finding["line"] else "")
        if need.lower() in line.lower():
            return True
    return False


def _decoy_flagged(finding: dict, decoy: dict) -> bool:
    if finding["file"] != decoy["file"]:
        return False
    anchor = decoy.get("anchor")
    return anchor is None or anchor in _line_text(finding["file"], finding["line"])


def score(findings: list[dict], gt: dict) -> dict:
    found = sorted({v["id"] for v in gt["violations"] for f in findings if _matches(f, v)},
                   key=lambda s: int(s[1:]))
    decoys = sorted({d["id"] for d in gt["decoys"] for f in findings if _decoy_flagged(f, d)})
    unmatched = [f for f in findings
                 if not any(_matches(f, v) for v in gt["violations"])
                 and not any(_decoy_flagged(f, d) for d in gt["decoys"])]
    return {
        "violations_found": len(found),
        "violations_total": len(gt["violations"]),
        "found_ids": found,
        "missed_ids": [v["id"] for v in gt["violations"] if v["id"] not in found],
        "decoys_flagged": decoys,
        "findings_to_review": len(findings),
        "unmatched_findings": len(unmatched),
    }


def main() -> int:
    gt = yaml.safe_load((ROOT / "benchmark" / "ground_truth.yaml").read_text())
    rows = {}
    for name, fn in (("keyword", keyword_findings), ("semgrep", semgrep_findings), ("amend", amend_findings)):
        t0 = time.perf_counter()
        findings = fn()
        secs = round(time.perf_counter() - t0, 1)
        if findings is None:
            rows[name] = {"skipped": "semgrep not installed or produced no JSON (pip install semgrep)"}
            continue
        rows[name] = {**score(findings, gt), "seconds": secs, "bobcoins": 0}

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    lines = ["| Condition | Violations found (of 13) | Missed | Decoys flagged | Findings to review | Seconds |",
             "|---|---|---|---|---|---|"]
    for name, r in rows.items():
        if "skipped" in r:
            lines.append(f"| {name} | skipped | | | | |")
            continue
        lines.append(f"| {name} | {r['violations_found']} ({', '.join(r['found_ids'])}) | "
                     f"{', '.join(r['missed_ids']) or 'none'} | {', '.join(r['decoys_flagged']) or 'none'} | "
                     f"{r['findings_to_review']} | {r['seconds']} |")
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
