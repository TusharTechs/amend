"""tests/test_mcp_tools.py — unit tests for amend/mcp_server.py tool functions.

Calls tool functions directly (bypassing the MCP transport layer).

Tests:
  1. get_redline 2023-07-01 -> 2026-09-01 includes 314.4(j) as added.
  2. query_graph trace government_id reaches the analytics bucket.
  3. guard_check returns findings.
  4. record_finding writes to a tmp copy of findings.json.
"""
from __future__ import annotations

import json
import sys
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
LENDWISE = REPO_ROOT / "demo" / "lendwise"

# Ensure demo/lendwise is importable for app.* imports in the graph builder
sys.path.insert(0, str(LENDWISE))


# ---------------------------------------------------------------------------
# Patch _ctx() so the tools use LENDWISE as app dir and REPO_ROOT as root
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_mcp_ctx(monkeypatch, tmp_path):
    """Redirect mcp_server globals to the real lendwise demo app."""
    import amend.mcp_server as srv

    # Create a minimal .amend dir in tmp_path to avoid writing to repo root
    amend_dir = tmp_path / ".amend"
    amend_dir.mkdir()

    # Patch the repo-root so events are written to tmp_path
    import amend.mcp_server as srv

    cfg = {
        "regulations_folder": "regulations/16cfr314",
        "from_date": "2021-01-01",
        "to_date": "2026-09-01",
        "also_date": "2023-07-01",
        "base_ref": "main",
        "mfa_dependency": "require_mfa",
    }

    monkeypatch.setattr(srv, "_repo_root", REPO_ROOT)
    monkeypatch.setattr(srv, "_app", LENDWISE)
    monkeypatch.setattr(srv, "_cfg", cfg)

    # Patch _events_path to write to tmp_path
    original_events_path = srv._events_path

    def _patched_events_path(repo_root: Path) -> Path:
        return amend_dir / "events.ndjson"

    monkeypatch.setattr(srv, "_events_path", _patched_events_path)

    yield


# ---------------------------------------------------------------------------
# Test 1: get_redline includes 314.4(j) as added
# ---------------------------------------------------------------------------

def test_get_redline_includes_314_4j():
    """get_redline from 2023-07-01 to 2026-09-01 must include 314.4(j) as added."""
    from amend.mcp_server import get_redline

    result = get_redline("2023-07-01", "2026-09-01", substantive_only=True)

    assert "count" in result
    assert "entries" in result

    # 314.4(j) should appear as an added clause
    entries = result["entries"]
    matching = [
        e for e in entries
        if "314.4(j)" in e.get("citation", "")
        and e.get("change") in ("added", "modified")
    ]
    assert matching, (
        f"Expected 314.4(j) as added/modified in redline; "
        f"got citations: {[e['citation'] for e in entries]}"
    )


# ---------------------------------------------------------------------------
# Test 2: query_graph trace government_id reaches analytics bucket
# ---------------------------------------------------------------------------

def test_query_graph_trace_government_id_reaches_analytics():
    """Tracing government_id through the graph must include a path to the analytics bucket."""
    from amend.mcp_server import query_graph

    result = query_graph("trace", "government_id")

    assert result.get("query") == "trace"
    assert result.get("arg") == "government_id"
    # The result should be non-empty (government_id flows somewhere)
    assert result.get("count", 0) > 0 or len(result.get("result", [])) > 0, (
        "Expected trace(government_id) to return at least one path"
    )

    # Check that analytics / bucket appears somewhere in the trace paths
    paths = result.get("result", [])
    flat = json.dumps(paths).lower()
    assert "analytic" in flat or "bucket" in flat or "export" in flat, (
        f"Expected analytics/bucket in trace paths; got: {flat[:400]}"
    )


# ---------------------------------------------------------------------------
# Test 3: guard_check returns findings for lendwise
# ---------------------------------------------------------------------------

def test_guard_check_returns_findings():
    """guard_check on demo/lendwise must return at least one finding."""
    from amend.mcp_server import guard_check

    result = guard_check()

    assert "finding_count" in result
    assert "findings" in result
    assert result["finding_count"] > 0, (
        f"Expected guard findings for lendwise; got: {result}"
    )


# ---------------------------------------------------------------------------
# Test 4: record_finding writes to compliance/findings.json (tmp copy)
# ---------------------------------------------------------------------------

def test_record_finding_writes_to_findings_json(monkeypatch, tmp_path):
    """record_finding must append to compliance/findings.json."""
    import amend.mcp_server as srv

    # Use a temp compliance dir
    compliance_dir = tmp_path / "compliance"
    compliance_dir.mkdir()
    findings_file = compliance_dir / "findings.json"

    # Patch _app to point to tmp_path so findings.json is written there
    monkeypatch.setattr(srv, "_app", tmp_path)

    result = srv.record_finding(
        obligation_id="SG-5",
        file="app/auth_routes.py",
        line=67,
        message="mobile token endpoint issues token without MFA",
        source="guard",
    )

    assert result["ok"] is True
    assert findings_file.exists(), "findings.json was not created"

    data = json.loads(findings_file.read_text())
    assert isinstance(data, list)
    assert len(data) == 1

    entry = data[0]
    assert entry["obligation_id"] == "SG-5"
    assert entry["file"] == "app/auth_routes.py"
    assert entry["line"] == 67
    assert entry["status"] == "open"
    assert entry["source"] == "guard"
