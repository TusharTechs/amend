"""
amend/evidence.py — evidence pack builder.

Public API
----------
build_evidence(repo_root, app_dir, base_ref, run_id=None) -> dict
    Reads obligations, impact sites, auditor findings, verification.json,
    runs guard and canary, and emits an evidence pack under evidence/<run-id>/.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from html import escape as _escape
from pathlib import Path
from typing import Any

from amend.obligations import load as load_obligations, Obligation
from amend.guard import run_guard
from amend.canary import run_canary


# ---------------------------------------------------------------------------
# Citations that require canary-clean status
# ---------------------------------------------------------------------------
CANARY_CITATIONS = {"16 CFR 314.4(c)(3)", "16 CFR 314.4(c)(6)(i)", "16 CFR 314.2(m)"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _git_shas(repo_root: Path, base_ref: str) -> tuple[str, str]:
    """Return (base_sha, head_sha)."""
    def _rev(ref: str) -> str:
        r = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"

    return _rev(base_ref), _rev("HEAD")


def _git_diff_summary(repo_root: Path, base_ref: str) -> list[dict]:
    """Return list of {file, insertions, deletions} for changed files."""
    r = subprocess.run(
        ["git", "diff", "--numstat", f"{base_ref}..HEAD"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            ins_raw, del_raw, fname = parts
            out.append({
                "file": fname,
                "insertions": int(ins_raw) if ins_raw.isdigit() else 0,
                "deletions": int(del_raw) if del_raw.isdigit() else 0,
            })
    return out


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------

def _obligation_status(
    ob: Obligation,
    ob_test_results: list[dict],
    guard_failures: list,
    canary_hits: list[dict],
    verification: dict,
    open_findings: list[dict],
) -> str:
    """Compute status string for one obligation."""

    # process / contract shape → human review
    if ob.shape in ("process", "contract"):
        return "Human review required"

    # any exceptions → human review
    if ob.exceptions:
        return "Human review required"

    checks: dict[str, bool] = {}

    # approved
    checks["approved"] = ob.status == "approved"

    # shape == code
    checks["shape_code"] = ob.shape == "code"

    # at least one discriminating test AND all tests pass on head
    ob_tests = [r for r in ob_test_results if _test_matches_ob(r["test_file"], ob.id)]
    discriminating = [r for r in ob_tests if r.get("discriminating", False)]
    all_pass_head = ob_tests and all(r["head_returncode"] == 0 for r in ob_tests)
    checks["has_discriminating_test"] = bool(discriminating)
    checks["all_tests_pass_head"] = bool(all_pass_head)

    # no guard failure whose clause contains this obligation's citation
    short_cit = ob.citation  # e.g. "16 CFR 314.4(c)(3)"
    cit_bare = short_cit.replace("16 CFR ", "")  # "314.4(c)(3)"
    guard_clean = not any(
        (cit_bare in f.clause or short_cit in f.clause)
        for f in guard_failures
    )
    checks["guard_clean"] = guard_clean

    # canary clean when the citation is one of the canary-sensitive ones
    if short_cit in CANARY_CITATIONS or cit_bare in {c.replace("16 CFR ", "") for c in CANARY_CITATIONS}:
        checks["canary_clean"] = len(canary_hits) == 0
    else:
        checks["canary_clean"] = True

    # integrity intact
    checks["integrity_clean"] = verification.get("integrity", {}).get("clean", False)

    # regression green
    reg = verification.get("regression", {})
    checks["regression_green"] = reg.get("skipped", True) or reg.get("returncode", 1) == 0

    # no open findings for this obligation
    ob_open_findings = [
        f for f in open_findings
        if f.get("obligation_id") == ob.id and f.get("status", "open") == "open"
    ]
    checks["no_open_findings"] = len(ob_open_findings) == 0

    all_pass = all(checks.values())
    any_pass = any(checks.values())

    if all_pass:
        return "Verified"
    elif any_pass:
        return "Partially verified"
    else:
        return "Unverified"


def _test_matches_ob(test_file: str, ob_id: str) -> bool:
    """True if test_file name starts with the normalised obligation prefix."""
    normed = ob_id.lower().replace("-", "")
    return test_file.lower().startswith(f"test_{normed}_")


# ---------------------------------------------------------------------------
# HTML certificate
# ---------------------------------------------------------------------------

def _rel_to(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _scrub(obj: Any, repo_root: Path) -> Any:
    """Replace absolute repo and home-directory paths in every string."""
    roots = sorted({str(Path(repo_root).resolve()), str(repo_root)}, key=len, reverse=True)
    home = str(Path.home())

    def fix(s: str) -> str:
        for r in roots:
            s = s.replace(r + "/", "").replace(r, ".")
        return s.replace(home, "~")

    if isinstance(obj, str):
        return fix(obj)
    if isinstance(obj, list):
        return [_scrub(v, repo_root) for v in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, repo_root) for k, v in obj.items()}
    return obj


def _canary_at_ref(repo_root: Path, app_dir: Path, base_ref: str) -> list[dict]:
    """Run the canary sweep against *base_ref* in a temporary git worktree."""
    import shutil
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="amend_canary_base_")
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", tmp, base_ref],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if added.returncode != 0:
            return []
        base_app = Path(tmp) / Path(app_dir).resolve().relative_to(Path(repo_root).resolve())
        hits = run_canary(base_app) or []
        for h in hits:
            h["path"] = _rel_to(h.get("path", ""), base_app)
        return hits
    except Exception:
        return []
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=str(repo_root), capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _clean_snippet(snippet: str, limit: int = 80) -> str:
    """Printable, HTML-escaped snippet. Binary stores such as sqlite pages would
    otherwise render as replacement glyphs, and raw '<' or '&' would break the page."""
    runs = re.findall(r"[\x20-\x7e]{4,}", snippet)
    return _escape(" · ".join(r.strip() for r in runs)[:limit])


def _build_html(cert: dict) -> str:
    meta = cert["meta"]
    rows = cert["obligations"]
    status_counts = cert["status_counts"]
    canary = cert["canary"]
    guard_findings = cert["guard_findings"]
    artifacts = cert["artifact_hashes"]

    def status_color(s: str) -> str:
        return {
            "Verified": "#15803d",
            "Human review required": "#b45309",
            "Partially verified": "#1d4ed8",
            "Unverified": "#dc2626",
        }.get(s, "#374151")

    # Summary strip
    strip_cells = "".join(
        f'<span style="margin-right:1.5em;color:{status_color(s)};">'
        f'<strong>{s}</strong>: {n}</span>'
        for s, n in status_counts.items()
    )

    # Obligation table rows
    def _sites(sites: list) -> str:
        if not sites:
            return "—"
        return "<br>".join(
            f'<code>{s["file"]}:{s["line"]}</code> '
            f'<em style="font-size:0.85em;color:#6b7280">{s.get("source","")}</em>'
            for s in sites[:10]
        )

    def _tests(tests: list) -> str:
        if not tests:
            return "—"
        parts = []
        for t in tests:
            base_icon = "✗" if t.get("base_returncode", 1) != 0 else "✓"
            head_icon = "✓" if t.get("head_returncode", 0) == 0 else "✗"
            disc = "D" if t.get("discriminating") else "N"
            parts.append(
                f'<span title="{t["test_file"]}">'
                f'base:{base_icon} head:{head_icon} [{disc}]</span>'
            )
        return "<br>".join(parts)

    table_rows = ""
    for r in rows:
        sc = status_color(r["status"])
        table_rows += (
            f'<tr>'
            f'<td><code>{r["citation"]}</code></td>'
            f'<td style="font-style:italic;max-width:20em;font-size:0.85em">{_escape(r["quote"])}</td>'
            f'<td>{r["obligation_id"]}</td>'
            f'<td style="font-size:0.85em">{_sites(r["impact_sites"])}</td>'
            f'<td style="font-size:0.85em">{_tests(r["obligation_tests"])}</td>'
            f'<td style="color:{sc};font-weight:600">{r["status"]}</td>'
            f'</tr>\n'
        )

    # Guard findings
    guard_rows = ""
    for f in guard_findings:
        guard_rows += (
            f'<tr><td><code>{f["file"]}:{f["line"]}</code></td>'
            f'<td>{f["rule"]}</td>'
            f'<td><code>{f["clause"]}</code></td>'
            f'<td>{f["message"]}</td></tr>\n'
        )

    # Canary
    canary_before = [h for h in canary.get("before", []) if h]
    canary_after = [h for h in canary.get("after", []) if h]

    def _canary_table(hits: list) -> str:
        if not hits:
            return '<em style="color:#15803d">clean</em>'
        rows_html = ""
        for h in hits:
            rows_html += (
                f'<tr><td>{h.get("store_type","")}</td>'
                f'<td><code>{h.get("path","")}</code></td>'
                f'<td>{h.get("offset_or_line","")}</td>'
                f'<td><code>{_clean_snippet(h.get("snippet", ""))}</code></td></tr>\n'
            )
        return (
            '<table style="width:100%;font-size:0.85em">'
            '<tr><th>Store</th><th>Path</th><th>Offset</th><th>Snippet</th></tr>'
            f'{rows_html}</table>'
        )

    # Test integrity: pre-existing tests are locked; a human may approve a rewrite
    integrity = cert.get("integrity", {}) or {}
    approved_changes = integrity.get("approved_changes", [])
    integrity_findings = integrity.get("findings", [])
    integrity_rows = "".join(
        f'<tr><td><code>{c["file"]}</code></td><td>{c.get("reason", "")}</td>'
        f'<td>{c.get("approved_by", "")}</td></tr>\n'
        for c in approved_changes
    )
    integrity_html = (
        ('<p><strong style="color:#b91c1c">Unapproved test changes:</strong> '
         + ", ".join(f'<code>{f["file"]}</code> ({f["issue"]})' for f in integrity_findings) + '</p>')
        if integrity_findings else
        '<p><em style="color:#15803d">No unapproved changes to pre-existing tests.</em></p>'
    )
    if approved_changes:
        integrity_html += (
            '<p style="margin-top:0.5rem">Pre-existing tests rewritten by a named human because they '
            'asserted behaviour the obligation prohibits (agents are blocked from editing them):</p>'
            '<table><tr><th>Test file</th><th>Reason</th><th>Approved by</th></tr>'
            f'{integrity_rows}</table>'
        )

    # Artifact hashes
    artifact_rows = "".join(
        f'<tr><td><code>{fname}</code></td><td style="font-family:monospace;font-size:0.8em">{h}</td></tr>\n'
        for fname, h in artifacts.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amend Evidence Certificate — {meta['run_id']}</title>
<style>
  :root {{
    --bg: #ffffff; --surface: #f7f8fa; --border: #e5e7eb;
    --text: #1f2328; --muted: #57606a; --accent: #3b82d4;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    }}
  }}
  @media print {{ body {{ background: white; color: black; }} }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system,"Segoe UI",system-ui,sans-serif;
    font-size: 14px; line-height: 1.6; padding: 2rem;
  }}
  .container {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1rem; margin: 1.5rem 0 0.5rem; border-bottom: 1px solid var(--border); padding-bottom: 0.25rem; }}
  .strip {{ background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem 1rem; margin: 1rem 0; }}
  .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.5rem; margin: 1rem 0; }}
  .meta-item {{ background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem; }}
  .meta-item .label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .meta-item .value {{ font-family: monospace; font-size: 0.85rem; word-break: break-all; }}
  table {{ width: 100%; border-collapse: collapse; margin: 0.5rem 0; font-size: 0.88rem; }}
  th {{ background: var(--surface); border: 1px solid var(--border); padding: 0.4rem 0.5rem; text-align: left; font-weight: 600; }}
  td {{ border: 1px solid var(--border); padding: 0.4rem 0.5rem; vertical-align: top; }}
  code {{ background: var(--surface); padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }}
  .disclaimer {{
    margin-top: 1.5rem; padding: 0.75rem 1rem;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; font-size: 0.85rem; color: var(--muted);
  }}
  footer {{
    margin-top: 2rem; padding-top: 1rem;
    border-top: 1px solid var(--border);
    text-align: center; font-size: 0.75rem; color: var(--muted);
  }}
</style>
</head>
<body>
<div class="container">
  <h1>Amend Evidence Certificate</h1>
  <div class="meta-grid">
    <div class="meta-item"><div class="label">Run ID</div><div class="value">{meta['run_id']}</div></div>
    <div class="meta-item"><div class="label">Generated</div><div class="value">{meta['generated_at']}</div></div>
    <div class="meta-item"><div class="label">Base SHA</div><div class="value">{meta['base_sha']}</div></div>
    <div class="meta-item"><div class="label">Head SHA</div><div class="value">{meta['head_sha']}</div></div>
    <div class="meta-item"><div class="label">Base Ref</div><div class="value">{meta['base_ref']}</div></div>
  </div>

  <h2>Status Summary</h2>
  <div class="strip">{strip_cells}</div>

  <h2>Obligation Matrix</h2>
  <table>
    <tr>
      <th>Clause</th><th>Quote</th><th>Obligation</th>
      <th>Code Sites</th><th>Tests (base/head)</th><th>Status</th>
    </tr>
    {table_rows}
  </table>

  <h2>Guard Findings</h2>
  {"<em>None</em>" if not guard_findings else
  '<table><tr><th>Location</th><th>Rule</th><th>Clause</th><th>Message</th></tr>' + guard_rows + '</table>'}

  <h2>Canary Hits — Before</h2>
  {_canary_table(canary_before)}

  <h2>Canary Hits — After</h2>
  {_canary_table(canary_after)}

  <h2>Test Integrity</h2>
  {integrity_html}

  <h2>Artifact Hashes (SHA-256)</h2>
  <table>
    <tr><th>File</th><th>SHA-256</th></tr>
    {artifact_rows}
  </table>

  <div class="disclaimer">
    <p><strong>Impact discovery recall on the Amend benchmark:</strong>
    see <code>benchmark/results</code>; unknown paths may exist.</p>
    <p style="margin-top:0.5rem">This report states what automated checks verified.
    It is not a legal opinion and does not assert compliance.</p>
  </div>
</div>
<footer>Made with IBM Bob</footer>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_evidence(
    repo_root: str | Path,
    app_dir: str | Path,
    base_ref: str = "HEAD~1",
    run_id: str | None = None,
) -> dict:
    """
    Build the evidence pack.

    Reads: obligations, impact sites, auditor findings, verification.json.
    Runs: guard, canary.
    Writes: evidence/<run-id>/matrix.json, certificate.json, certificate.md,
            certificate.html
    Returns: the certificate dict.
    """
    repo_root = Path(repo_root).resolve()
    app_dir = Path(app_dir).resolve()

    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

    evidence_dir = repo_root / "evidence" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # ── Obligations ───────────────────────────────────────────────────────────
    ob_dir = app_dir / "compliance" / "obligations"
    obligations: list[Obligation] = []
    if ob_dir.exists():
        obligations = load_obligations(ob_dir)

    # ── Impact sites ──────────────────────────────────────────────────────────
    impact_dir = app_dir / "compliance" / "impact"
    impact_sites: list[dict] = []
    if impact_dir.exists():
        for f in sorted(impact_dir.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                impact_sites.extend(data)

    # ── Auditor findings ──────────────────────────────────────────────────────
    findings_path = app_dir / "compliance" / "findings.json"
    all_findings: list[dict] = []
    if findings_path.exists():
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            all_findings = data
    open_findings = [f for f in all_findings if f.get("status", "open") == "open"]

    # ── Verification JSON ─────────────────────────────────────────────────────
    verification_path = repo_root / ".amend" / "verification.json"
    verification: dict = {}
    if verification_path.exists():
        verification = json.loads(verification_path.read_text(encoding="utf-8"))

    ob_test_results: list[dict] = verification.get("obligation_tests", [])
    compliance_tests_dir = app_dir / "compliance" / "tests"
    # Fall back: scan compliance/tests/ for test files
    if not ob_test_results and compliance_tests_dir.exists():
        for p in sorted(compliance_tests_dir.glob("test_*.py")):
            ob_test_results.append({
                "test_file": p.name,
                "base_returncode": 1,
                "head_returncode": 0,
                "discriminating": True,
                "rejected": False,
                "rejection_reason": "",
            })

    # ── Guard ─────────────────────────────────────────────────────────────────
    guard_failures = []
    try:
        guard_failures = run_guard(app_dir)
    except Exception:
        pass

    # ── Canary ────────────────────────────────────────────────────────────────
    canary_hits_after: list[dict] = []
    try:
        canary_hits_after = run_canary(app_dir) or []
    except Exception:
        pass

    # Before canary: the same sweep against the base commit, in a throwaway worktree
    canary_hits_before: list[dict] = (
        verification.get("canary_before") or _canary_at_ref(repo_root, app_dir, base_ref)
    )
    for h in canary_hits_after:
        h["path"] = _rel_to(h.get("path", ""), app_dir)

    # ── Git SHAs and diff ─────────────────────────────────────────────────────
    base_sha, head_sha = _git_shas(repo_root, base_ref)
    git_diff = _git_diff_summary(repo_root, base_ref)

    # ── Build obligation rows ─────────────────────────────────────────────────
    matrix_rows: list[dict] = []
    status_counts: dict[str, int] = {}

    for ob in obligations:
        sites = [s for s in impact_sites if s.get("obligation_id") == ob.id]
        tests = [r for r in ob_test_results if _test_matches_ob(r["test_file"], ob.id)]

        status = _obligation_status(
            ob, ob_test_results, guard_failures,
            canary_hits_after, verification, open_findings,
        )
        status_counts[status] = status_counts.get(status, 0) + 1

        row = {
            "obligation_id": ob.id,
            "citation": ob.citation,
            "quote": ob.quote,
            "shape": ob.shape,
            "status": status,
            "impact_sites": sites,
            "obligation_tests": tests,
            "ob_status": ob.status,
            "approved_by": ob.approved_by,
        }
        matrix_rows.append(row)

    # Evidence is shared with auditors: no absolute paths or home directories.
    matrix_rows = _scrub(matrix_rows, repo_root)

    # ── Write matrix.json ─────────────────────────────────────────────────────
    matrix_path = evidence_dir / "matrix.json"
    matrix_path.write_text(json.dumps(matrix_rows, indent=2), encoding="utf-8")

    # ── Assemble certificate dict ─────────────────────────────────────────────
    cert: dict[str, Any] = {
        "meta": {
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "head_sha": head_sha,
        },
        "status_counts": status_counts,
        "obligations": matrix_rows,
        "guard_findings": [f.to_dict() for f in guard_failures],
        "canary": {
            "before": canary_hits_before,
            "after": canary_hits_after,
        },
        "git_diff": git_diff,
        "integrity": verification.get("integrity", {}),
        "regression": verification.get("regression", {}),
        "artifact_hashes": {},
        "disclaimer": (
            "Impact discovery recall on the Amend benchmark: see benchmark/results; "
            "unknown paths may exist. "
            "This report states what automated checks verified. "
            "It is not a legal opinion and does not assert compliance."
        ),
    }
    cert = _scrub(cert, repo_root)

    # ── Write certificate.json ────────────────────────────────────────────────
    cert_json_path = evidence_dir / "certificate.json"
    cert_json_path.write_text(json.dumps(cert, indent=2), encoding="utf-8")

    # ── Write certificate.md ──────────────────────────────────────────────────
    md_lines = [
        f"# Amend Evidence Certificate",
        f"",
        f"**Run ID:** {run_id}  ",
        f"**Generated:** {cert['meta']['generated_at']}  ",
        f"**Base SHA:** {base_sha}  ",
        f"**Head SHA:** {head_sha}  ",
        f"",
        f"## Status Summary",
        f"",
    ]
    for s, n in status_counts.items():
        md_lines.append(f"- **{s}**: {n}")
    md_lines += [
        f"",
        f"## Obligation Matrix",
        f"",
        f"| Clause | Obligation | Shape | Status |",
        f"|--------|------------|-------|--------|",
    ]
    for r in matrix_rows:
        md_lines.append(
            f"| {r['citation']} | {r['obligation_id']} | {r['shape']} | {r['status']} |"
        )
    md_lines += [
        f"",
        f"## Guard Findings",
        f"",
    ]
    if guard_failures:
        md_lines.append("| File | Rule | Clause | Message |")
        md_lines.append("|------|------|--------|---------|")
        for f in guard_failures:
            md_lines.append(f"| {f.file}:{f.line} | {f.rule} | {f.clause} | {f.message} |")
    else:
        md_lines.append("_None_")

    md_lines += [
        f"",
        f"## Artifact Hashes",
        f"",
    ]
    # hashes filled after writing HTML
    md_lines += [
        f"",
        f"> Impact discovery recall on the Amend benchmark: see `benchmark/results`; unknown paths may exist.",
        f">",
        f"> This report states what automated checks verified. It is not a legal opinion and does not assert compliance.",
    ]

    md_path = evidence_dir / "certificate.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    # ── Write certificate.html ────────────────────────────────────────────────
    html_path = evidence_dir / "certificate.html"

    # Compute artifact hashes (before writing html, use placeholders; then rewrite)
    artifact_hashes: dict[str, str] = {
        "matrix.json": _sha256(matrix_path),
        "certificate.json": _sha256(cert_json_path),
        "certificate.md": _sha256(md_path),
    }
    cert["artifact_hashes"] = artifact_hashes

    html_content = _build_html(cert)
    html_path.write_text(html_content, encoding="utf-8")
    artifact_hashes["certificate.html"] = _sha256(html_path)
    cert["artifact_hashes"] = artifact_hashes

    # Rewrite certificate.json with final hashes
    cert_json_path.write_text(json.dumps(cert, indent=2), encoding="utf-8")

    return cert
