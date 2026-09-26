"""
tests/test_verify.py — tests for amend/verify.py

Uses a tiny throwaway git repo in tmp_path:
  - A discriminating test (fails on base, passes on head) is accepted.
  - A test that already passes on base is rejected.
  - Editing a pre-existing non-compliance test is flagged.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from amend.verify import verify, _sha256, _count_asserts, _has_skip_xfail


# ---------------------------------------------------------------------------
# Helpers to build a minimal git repo in tmp_path
# ---------------------------------------------------------------------------

def _init_repo(root: Path) -> None:
    """Create a bare git repo with an initial commit."""
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True, capture_output=True)


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", message],
                   check=True, capture_output=True)


def _make_app_skeleton(app_dir: Path) -> None:
    """Create minimal app structure with compliance/tests/ and a tests/ folder."""
    (app_dir / "compliance" / "tests").mkdir(parents=True)
    (app_dir / "compliance" / "obligations").mkdir(parents=True)
    (app_dir / "tests").mkdir(parents=True)
    # A pre-existing app test (not in compliance/)
    (app_dir / "tests" / "__init__.py").write_text("")
    (app_dir / "tests" / "test_hello.py").write_text(
        "def test_hello():\n    assert 1 + 1 == 2\n"
    )


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------

def test_sha256_is_deterministic(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")
    assert _sha256(f) == _sha256(f)


def test_count_asserts():
    src = "def f():\n    assert x == 1\n    assert y == 2\n"
    assert _count_asserts(src) == 2


def test_count_asserts_zero():
    assert _count_asserts("def f(): pass\n") == 0


def test_has_skip_xfail_true():
    assert _has_skip_xfail("pytest.skip('not ready')")


def test_has_skip_xfail_false():
    assert not _has_skip_xfail("def test_ok(): assert True")


# ---------------------------------------------------------------------------
# Integration tests using a real tmp git repo
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    """
    Build a minimal repo:
      base commit: app/tests/test_hello.py (passes), no compliance tests
      head commit (working tree): add a discriminating compliance test
    Returns (repo_root, app_dir).
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    app = root / "myapp"
    _make_app_skeleton(app)
    _commit_all(root, "initial")

    return root, app


def test_no_obligation_tests_returns_empty(repo):
    """With no compliance tests, obligation_tests list is empty."""
    root, app = repo
    result = verify(root, app, base_ref="HEAD")
    assert result["obligation_tests"] == []


def test_verification_json_written(repo):
    """verify() must write .amend/verification.json."""
    root, app = repo
    verify(root, app, base_ref="HEAD")
    out = root / ".amend" / "verification.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert "obligation_tests" in data
    assert "integrity" in data
    assert "regression" in data
    assert "summary" in data


def test_discriminating_test_accepted(repo):
    """
    A test that FAILS on base and PASSES on head is discriminating and not rejected.
    We achieve this by adding the compliance test only on HEAD.
    On base the test file doesn't exist → rc=1 (fail), on head it passes → rc=0.
    """
    root, app = repo
    # Write a passing compliance test (added only now, not on base)
    ob_test = app / "compliance" / "tests" / "test_sg1_always_true.py"
    ob_test.write_text("def test_sg1_ok():\n    assert True\n")

    result = verify(root, app, base_ref="HEAD")
    assert len(result["obligation_tests"]) == 1
    r = result["obligation_tests"][0]
    assert r["discriminating"] is True
    assert r["rejected"] is False
    assert r["head_returncode"] == 0
    # base_returncode is 1 because the file didn't exist on base
    assert r["base_returncode"] == 1


def test_non_discriminating_test_rejected(repo):
    """
    A test that passes on both base and head is non-discriminating and rejected.
    We commit the compliance test to base so it exists and passes there too.
    """
    root, app = repo

    # Add compliance test and commit it to base
    ob_test = app / "compliance" / "tests" / "test_sg2_trivial.py"
    ob_test.write_text("def test_sg2_passes():\n    assert True\n")
    _commit_all(root, "add compliance test")

    # Now HEAD == the new commit; base_ref = HEAD~1 (before compliance test)
    # Wait — if we committed the test, it exists on both. Let's use HEAD as base_ref
    # so the test exists on base too.
    result = verify(root, app, base_ref="HEAD")
    assert len(result["obligation_tests"]) == 1
    r = result["obligation_tests"][0]
    assert r["rejected"] is True
    assert r["rejection_reason"] != ""


def test_integrity_flags_modified_test(repo):
    """
    If a pre-existing non-compliance test is modified on HEAD, integrity flags it.
    """
    root, app = repo

    # Stage 1: commit the test file as-is on base
    # (it was already committed as part of initial; HEAD is "initial")
    base_ref = "HEAD"

    # Now modify the pre-existing test on the working tree
    test_file = app / "tests" / "test_hello.py"
    test_file.write_text("def test_hello():\n    assert 2 + 2 == 4\n    # modified\n")

    result = verify(root, app, base_ref=base_ref)
    findings = result["integrity"]["findings"]
    modified = [f for f in findings if f["issue"] == "modified"]
    assert any("test_hello.py" in f["file"] for f in modified)


def test_integrity_flags_deleted_test(repo):
    """
    If a pre-existing test file is deleted on HEAD, integrity flags it.
    """
    root, app = repo
    base_ref = "HEAD"

    (app / "tests" / "test_hello.py").unlink()

    result = verify(root, app, base_ref=base_ref)
    findings = result["integrity"]["findings"]
    deleted = [f for f in findings if f["issue"] == "deleted"]
    assert len(deleted) >= 1


def test_regression_skipped_when_no_tests_dir(tmp_path):
    """When app has no tests/ folder, regression is marked skipped."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    app = root / "myapp"
    (app / "compliance" / "tests").mkdir(parents=True)
    # Need something to commit
    (app / "compliance" / "tests" / ".gitkeep").write_text("")
    _commit_all(root, "init")

    result = verify(root, app, base_ref="HEAD")
    assert result["regression"]["skipped"] is True
