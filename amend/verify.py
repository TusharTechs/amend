"""
amend/verify.py — fail-before / pass-after proof engine.

Public API
----------
verify(repo_root, app_dir, base_ref) -> dict
    Runs obligation tests, integrity checks, and regression suite.
    Writes .amend/verification.json and returns the result dict.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _count_asserts(source: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))


def _has_skip_xfail(source: str) -> bool:
    return bool(re.search(r"pytest\.(?:skip|xfail|mark\.skip|mark\.xfail)", source))


def _obligation_test_pattern(ob_id: str) -> str:
    """Return glob pattern for a given obligation id."""
    normed = ob_id.lower().replace("-", "")
    return f"test_{normed}_*.py"


def _run_pytest(test_paths: list[str], cwd: Path) -> tuple[int, str]:
    """Run pytest on *test_paths* inside *cwd*. Returns (returncode, output)."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--no-header"] + test_paths
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def _git_worktree_add(repo_root: Path, base_ref: str) -> Path:
    """Create a temp git worktree at *base_ref* and return its path."""
    tmp = tempfile.mkdtemp(prefix="amend_worktree_")
    subprocess.run(
        ["git", "worktree", "add", "--detach", tmp, base_ref],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    return Path(tmp)


def _git_worktree_remove(repo_root: Path, worktree_path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo_root),
        capture_output=True,
    )


def _list_test_files(app_dir: Path) -> list[Path]:
    """Return all test *.py files under app_dir that are NOT in compliance/tests/."""
    result = []
    compliance_tests = app_dir / "compliance" / "tests"
    for p in app_dir.rglob("test_*.py"):
        try:
            p.relative_to(compliance_tests)
            continue  # inside compliance/tests/ — skip
        except ValueError:
            pass
        result.append(p)
    return sorted(result)


# ---------------------------------------------------------------------------
# Core verify
# ---------------------------------------------------------------------------

def verify(repo_root: str | Path, app_dir: str | Path, base_ref: str = "HEAD~1") -> dict:
    """
    Run the full proof-engine pipeline.

    Returns a dict with keys:
      obligation_tests, integrity, regression, summary
    Also writes <repo_root>/.amend/verification.json.
    """
    repo_root = Path(repo_root).resolve()
    app_dir = Path(app_dir).resolve()

    compliance_tests_dir = app_dir / "compliance" / "tests"

    # ── 1. Collect obligation test files ──────────────────────────────────────
    ob_test_files: list[Path] = sorted(compliance_tests_dir.glob("test_*.py")) \
        if compliance_tests_dir.exists() else []

    # ── 2. Integrity: snapshot non-compliance test files on HEAD ─────────────
    head_test_files = _list_test_files(app_dir)
    head_hashes: dict[str, str] = {}
    head_asserts: dict[str, int] = {}
    head_skipxfail: dict[str, bool] = {}
    for p in head_test_files:
        rel = str(p.relative_to(app_dir))
        head_hashes[rel] = _sha256(p)
        src = p.read_text(encoding="utf-8")
        head_asserts[rel] = _count_asserts(src)
        head_skipxfail[rel] = _has_skip_xfail(src)

    # ── 3. Worktree at base_ref for fail-before ───────────────────────────────
    worktree: Path | None = None
    obligation_results: list[dict] = []

    # Integrity: base test files
    integrity_findings: list[dict] = []
    base_test_hashes: dict[str, str] = {}
    base_asserts: dict[str, int] = {}

    try:
        worktree = _git_worktree_add(repo_root, base_ref)
        base_app_dir = worktree / app_dir.relative_to(repo_root)

        # Snapshot base test files
        base_test_files = _list_test_files(base_app_dir)
        for p in base_test_files:
            rel = str(p.relative_to(base_app_dir))
            base_test_hashes[rel] = _sha256(p)
            src = p.read_text(encoding="utf-8")
            base_asserts[rel] = _count_asserts(src)

        # Integrity checks
        for rel, base_hash in base_test_hashes.items():
            if rel not in head_hashes:
                integrity_findings.append({"file": rel, "issue": "deleted"})
            elif head_hashes[rel] != base_hash:
                integrity_findings.append({"file": rel, "issue": "modified"})
            elif head_asserts.get(rel, 0) < base_asserts.get(rel, 0):
                integrity_findings.append({"file": rel, "issue": "assert_count_decreased"})
            elif head_skipxfail.get(rel, False):
                integrity_findings.append({"file": rel, "issue": "skip_or_xfail_added"})

        # Obligation tests: fail-before / pass-after
        for ob_test_path in ob_test_files:
            test_rel = ob_test_path.name
            base_compliance_tests = base_app_dir / "compliance" / "tests"
            base_ob_test = base_compliance_tests / test_rel

            ob_result: dict[str, Any] = {
                "test_file": test_rel,
                "base_returncode": None,
                "head_returncode": None,
                "base_output": "",
                "head_output": "",
                "discriminating": False,
                "rejected": False,
                "rejection_reason": "",
            }

            # Run on base (expect FAIL)
            if base_ob_test.exists():
                rc_base, out_base = _run_pytest([str(base_ob_test)], cwd=base_app_dir)
            else:
                # test didn't exist on base — treat as new
                rc_base, out_base = 1, "(test did not exist on base)"
            ob_result["base_returncode"] = rc_base
            ob_result["base_output"] = out_base

            # Run on head (expect PASS)
            rc_head, out_head = _run_pytest([str(ob_test_path)], cwd=app_dir)
            ob_result["head_returncode"] = rc_head
            ob_result["head_output"] = out_head

            # Discrimination check: passes on base → non-discriminating → reject
            if rc_base == 0:
                ob_result["rejected"] = True
                ob_result["rejection_reason"] = "non-discriminating: already passes on base"
            else:
                ob_result["discriminating"] = True

            obligation_results.append(ob_result)

    finally:
        if worktree is not None:
            _git_worktree_remove(repo_root, worktree)

    # ── 4. Regression: run app tests/ on HEAD ────────────────────────────────
    app_tests_dir = app_dir / "tests"
    regression: dict[str, Any] = {"skipped": True, "returncode": None, "output": ""}
    if app_tests_dir.exists():
        rc_reg, out_reg = _run_pytest([str(app_tests_dir)], cwd=app_dir)
        regression = {"skipped": False, "returncode": rc_reg, "output": out_reg}

    # ── 5. Assemble result ────────────────────────────────────────────────────
    discriminating_passing = [
        r for r in obligation_results
        if r["discriminating"] and r["head_returncode"] == 0
    ]
    all_pass_head = all(r["head_returncode"] == 0 for r in obligation_results)
    regression_green = regression["skipped"] or regression["returncode"] == 0
    integrity_clean = len(integrity_findings) == 0

    result: dict[str, Any] = {
        "base_ref": base_ref,
        "obligation_tests": obligation_results,
        "integrity": {
            "findings": integrity_findings,
            "clean": integrity_clean,
        },
        "regression": regression,
        "summary": {
            "total_obligation_tests": len(obligation_results),
            "discriminating": len([r for r in obligation_results if r["discriminating"]]),
            "rejected": len([r for r in obligation_results if r["rejected"]]),
            "passing_head": len([r for r in obligation_results if r["head_returncode"] == 0]),
            "discriminating_passing": len(discriminating_passing),
            "integrity_clean": integrity_clean,
            "regression_green": regression_green,
            "all_obligation_tests_pass_head": all_pass_head,
        },
    }

    # ── 6. Write .amend/verification.json ────────────────────────────────────
    out_dir = repo_root / ".amend"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "verification.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return result
