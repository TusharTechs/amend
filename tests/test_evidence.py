"""
tests/test_evidence.py — tests for amend/evidence.py

Uses a tiny throwaway git repo in tmp_path.
Key assertions:
  - A process-shape obligation is marked "Human review required".
  - A fully passing code-shape obligation is marked "Verified".
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from amend.evidence import build_evidence, _obligation_status, _test_matches_ob
from amend.obligations import Obligation


# ---------------------------------------------------------------------------
# Pure unit helpers
# ---------------------------------------------------------------------------

def test_test_matches_ob_positive():
    assert _test_matches_ob("test_sg1staffloginrequiresmfa_check.py", "SG1-STAFF-LOGIN-REQUIRES-MFA")


def test_test_matches_ob_negative():
    assert not _test_matches_ob("test_other_thing.py", "SG1-STAFF-LOGIN-REQUIRES-MFA")


def _make_obligation(ob_id="OB1", shape="code", status="approved", citation="16 CFR 314.4(c)(5)",
                     exceptions=None, approved_by="alice") -> Obligation:
    return Obligation(
        id=ob_id,
        citation=citation,
        version="2026-09-01",
        change="modified",
        quote="Implement multi-factor authentication",
        statement="MFA required",
        shape=shape,
        status=status,
        exceptions=exceptions or [],
        approved_by=approved_by,
    )


def _default_verification(integrity_clean=True, regression_green=True) -> dict:
    return {
        "obligation_tests": [],
        "integrity": {"clean": integrity_clean, "findings": []},
        "regression": {"skipped": False, "returncode": 0 if regression_green else 1, "output": ""},
        "summary": {},
    }


def test_process_obligation_requires_human_review():
    ob = _make_obligation(shape="process")
    status = _obligation_status(ob, [], [], [], _default_verification(), [])
    assert status == "Human review required"


def test_contract_obligation_requires_human_review():
    ob = _make_obligation(shape="contract")
    status = _obligation_status(ob, [], [], [], _default_verification(), [])
    assert status == "Human review required"


def test_obligation_with_exceptions_requires_human_review():
    ob = _make_obligation(shape="code", exceptions=["unless approved in writing"])
    status = _obligation_status(ob, [], [], [], _default_verification(), [])
    assert status == "Human review required"


def test_code_obligation_verified_when_all_checks_pass():
    ob = _make_obligation(shape="code", status="approved", approved_by="alice",
                          citation="16 CFR 314.4(c)(5)")
    # One discriminating test that passes on head
    ob_tests = [{
        "test_file": "test_ob1_mfa.py",
        "base_returncode": 1,
        "head_returncode": 0,
        "discriminating": True,
        "rejected": False,
    }]
    verification = _default_verification(integrity_clean=True, regression_green=True)
    status = _obligation_status(ob, ob_tests, [], [], verification, [])
    assert status == "Verified"


def test_code_obligation_unverified_when_no_tests():
    ob = _make_obligation(shape="code", status="approved", approved_by="alice")
    status = _obligation_status(ob, [], [], [], _default_verification(), [])
    assert status in ("Unverified", "Partially verified")


def test_code_obligation_partially_verified_when_test_fails_head():
    ob = _make_obligation(shape="code", status="approved", approved_by="alice",
                          citation="16 CFR 314.4(c)(5)")
    ob_tests = [{
        "test_file": "test_ob1_mfa.py",
        "base_returncode": 1,
        "head_returncode": 1,  # still failing on head
        "discriminating": True,
        "rejected": False,
    }]
    verification = _default_verification()
    status = _obligation_status(ob, ob_tests, [], [], verification, [])
    assert status in ("Partially verified", "Unverified")


def test_open_finding_prevents_verified():
    ob = _make_obligation(shape="code", status="approved", approved_by="alice",
                          citation="16 CFR 314.4(c)(5)")
    ob_tests = [{
        "test_file": "test_ob1_mfa.py",
        "base_returncode": 1,
        "head_returncode": 0,
        "discriminating": True,
        "rejected": False,
    }]
    verification = _default_verification()
    open_findings = [{"obligation_id": "OB1", "status": "open", "message": "issue"}]
    status = _obligation_status(ob, ob_tests, [], [], verification, open_findings)
    assert status != "Verified"


# ---------------------------------------------------------------------------
# Integration: build_evidence with a tiny git repo
# ---------------------------------------------------------------------------

def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"],
                   check=True, capture_output=True)


def _commit_all(root: Path, msg: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", msg],
                   check=True, capture_output=True)


@pytest.fixture()
def evidence_repo(tmp_path):
    """
    Minimal repo with:
      - One code obligation (approved, no exceptions) → should become Verified
        (given we inject a discriminating passing test into verification.json)
      - One process obligation → should become Human review required
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    app = root / "app"
    ob_dir = app / "compliance" / "obligations"
    ob_dir.mkdir(parents=True)
    (app / "compliance" / "tests").mkdir(parents=True)
    (app / "compliance" / "impact").mkdir(parents=True)

    # Code obligation
    code_ob = {
        "id": "OB-CODE-1",
        "citation": "16 CFR 314.4(c)(5)",
        "version": "2026-09-01",
        "change": "modified",
        "quote": "Implement multi-factor authentication",
        "statement": "MFA required for all staff",
        "shape": "code",
        "status": "approved",
        "exceptions": [],
        "approved_by": "compliance-team",
    }
    # Process obligation
    proc_ob = {
        "id": "OB-PROC-1",
        "citation": "16 CFR 314.4(a)",
        "version": "2026-09-01",
        "change": "modified",
        "quote": "Develop, implement, and maintain",
        "statement": "Written information security program required",
        "shape": "process",
        "status": "approved",
        "exceptions": [],
        "approved_by": "compliance-team",
    }
    (ob_dir / "obligations.yaml").write_text(
        yaml.dump({"obligations": [code_ob, proc_ob]}), encoding="utf-8"
    )

    # Impact site for code obligation
    (app / "compliance" / "impact" / "ob_code_1.json").write_text(
        json.dumps([{
            "obligation_id": "OB-CODE-1",
            "file": "app/auth.py",
            "line": 42,
            "kind": "enforcement",
            "source": "graph",
            "rationale": "MFA check",
        }]),
        encoding="utf-8",
    )

    _commit_all(root, "initial")

    # Inject a fake verification.json with a discriminating passing test for OB-CODE-1
    amend_dir = root / ".amend"
    amend_dir.mkdir()
    verification = {
        "base_ref": "HEAD",
        "obligation_tests": [{
            "test_file": "test_obcode1_mfa.py",
            "base_returncode": 1,
            "head_returncode": 0,
            "discriminating": True,
            "rejected": False,
            "rejection_reason": "",
        }],
        "integrity": {"clean": True, "findings": []},
        "regression": {"skipped": True, "returncode": None, "output": ""},
        "summary": {},
    }
    (amend_dir / "verification.json").write_text(
        json.dumps(verification), encoding="utf-8"
    )

    return root, app


def test_process_obligation_marked_human_review(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-01")
    ob_rows = {r["obligation_id"]: r for r in cert["obligations"]}
    assert ob_rows["OB-PROC-1"]["status"] == "Human review required"


def test_code_obligation_verified(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-02")
    ob_rows = {r["obligation_id"]: r for r in cert["obligations"]}
    assert ob_rows["OB-CODE-1"]["status"] == "Verified"


def test_evidence_files_written(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-03")
    ev_dir = root / "evidence" / "test-run-03"
    assert (ev_dir / "matrix.json").exists()
    assert (ev_dir / "certificate.json").exists()
    assert (ev_dir / "certificate.md").exists()
    assert (ev_dir / "certificate.html").exists()


def test_certificate_has_disclaimer(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-04")
    ev_dir = root / "evidence" / "test-run-04"
    html = (ev_dir / "certificate.html").read_text()
    assert "not a legal opinion" in html
    assert "benchmark/results" in html


def test_certificate_has_artifact_hashes(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-05")
    assert "matrix.json" in cert["artifact_hashes"]
    assert "certificate.json" in cert["artifact_hashes"]
    assert "certificate.html" in cert["artifact_hashes"]


def test_status_counts_present(evidence_repo):
    root, app = evidence_repo
    cert = build_evidence(root, app, base_ref="HEAD", run_id="test-run-06")
    assert isinstance(cert["status_counts"], dict)
    total = sum(cert["status_counts"].values())
    assert total == 2  # one code + one process obligation


def test_scrub_removes_absolute_repo_and_home_paths(tmp_path):
    from pathlib import Path
    from amend.evidence import _scrub

    home = str(Path.home())
    data = {"path": f"{tmp_path}/demo/lendwise/var/x.db", "out": [f"{home}/.venv/lib/site.py:1"]}
    clean = _scrub(data, tmp_path)
    assert clean["path"] == "demo/lendwise/var/x.db"
    assert clean["out"][0].startswith("~/")
    assert str(tmp_path) not in str(clean) and home not in str(clean)


def test_certificate_snippets_are_printable_and_escaped():
    from amend.evidence import _clean_snippet

    raw = "\x00\x02#\x1fB\ufffdq\x08A Y000-**-****<b>&x\x00pending"
    out = _clean_snippet(raw)
    assert "\ufffd" not in out and "\x00" not in out
    assert "&lt;b&gt;&amp;x" in out
    assert "000-**-****" in out
