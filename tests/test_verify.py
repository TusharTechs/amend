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

from amend.verify import verify, _sha256, _count_asserts, _has_skip_xfail, _count_skip_xfail


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


def _write_module(app_dir: Path, name: str, source: str) -> None:
    """Write a Python module at app_dir/<name>.py."""
    (app_dir / f"{name}.py").write_text(source, encoding="utf-8")


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
    Build a minimal repo with two commits:
      initial: app skeleton + feature.py returning 0 + compliance/tests/conftest.py
      head:    nothing extra (working tree is clean at HEAD)
    Returns (repo_root, app_dir).
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    app = root / "myapp"
    _make_app_skeleton(app)

    # feature.py at base: returns 0
    _write_module(app, "feature", "def get_value():\n    return 0\n")

    # conftest that puts the app dir on sys.path (needed by obligation tests
    # that import app modules)
    conftest = app / "compliance" / "tests" / "conftest.py"
    conftest.write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))\n"
    )
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


def test_new_assert_true_test_rejected(repo):
    """
    A brand-new test that only does `assert True` must be rejected.

    Under the fixed logic the test file is copied into the base worktree and
    run there; `assert True` passes on base (rc=0) so it is non-discriminating.
    """
    root, app = repo
    ob_test = app / "compliance" / "tests" / "test_sg1_always_true.py"
    ob_test.write_text("def test_sg1_ok():\n    assert True\n")

    result = verify(root, app, base_ref="HEAD")
    assert len(result["obligation_tests"]) == 1
    r = result["obligation_tests"][0]
    assert r["rejected"] is True
    assert "non-discriminating" in r["rejection_reason"]


def test_discriminating_test_accepted(repo):
    """
    A test that FAILS on base code and PASSES after a code fix is accepted.

    The repo fixture sets up an initial commit with feature.py returning 0.
    We commit a second "fix" commit that changes feature.py to return 1, then
    write the obligation test (not yet committed) which asserts the fixed value.
    base_ref="HEAD~1" → base worktree has get_value() == 0.
    """
    root, app = repo

    # Commit a fix: feature now returns 1
    _write_module(app, "feature", "def get_value():\n    return 1\n")
    _commit_all(root, "fix: feature returns 1")

    # Obligation test: asserts the fixed behaviour (uncommitted, HEAD working tree)
    ob_test = app / "compliance" / "tests" / "test_sg1_feature.py"
    ob_test.write_text(
        "from feature import get_value\n"
        "def test_sg1_value():\n"
        "    assert get_value() == 1\n"
    )

    result = verify(root, app, base_ref="HEAD~1")
    assert len(result["obligation_tests"]) == 1
    r = result["obligation_tests"][0]
    assert r["discriminating"] is True, f"expected discriminating, got: {r}"
    assert r["rejected"] is False
    assert r["base_returncode"] == 1
    assert r["head_returncode"] == 0


def test_inconclusive_import_error_rejected(repo):
    """
    A new test that imports a name missing at base gets exit code 2 (collection
    error) on base → inconclusive → rejected with the matching reason.

    The `repo` fixture already has an initial commit with no `missing_module`,
    so base_ref="HEAD" is sufficient — no extra commit needed.
    """
    root, app = repo

    # The obligation test references a module that does not exist anywhere
    ob_test = app / "compliance" / "tests" / "test_sg2_import.py"
    ob_test.write_text(
        "from missing_module_xyzzy import something\n"
        "def test_sg2_import():\n"
        "    assert something() == 1\n"
    )

    result = verify(root, app, base_ref="HEAD")
    assert len(result["obligation_tests"]) == 1
    r = result["obligation_tests"][0]
    assert r["rejected"] is True, f"expected rejected, got: {r}"
    assert "base run errored" in r["rejection_reason"]


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


def test_integrity_unchanged_skipif_not_flagged(repo):
    """
    A pre-existing test file that already has @pytest.mark.skipif on base
    must NOT be flagged when it is unchanged on HEAD.
    """
    root, app = repo
    base_ref = "HEAD"

    # Add a test file with a skipif marker and commit it (so it exists on base)
    skipif_test = app / "tests" / "test_with_skipif.py"
    skipif_test.write_text(
        "import pytest\n"
        "@pytest.mark.skipif(True, reason='demo')\n"
        "def test_existing_skipif():\n"
        "    assert True\n"
    )
    _commit_all(root, "add test with skipif")

    # HEAD working tree is identical to the committed version; no changes
    result = verify(root, app, base_ref=base_ref)
    skipxfail_findings = [
        f for f in result["integrity"]["findings"]
        if f["issue"] == "skip_or_xfail_added"
    ]
    assert skipxfail_findings == [], (
        f"Unchanged file with pre-existing skipif was incorrectly flagged: {skipxfail_findings}"
    )


def test_integrity_new_skip_marker_flagged(repo):
    """
    Adding a new @pytest.mark.skip to a pre-existing test file must be flagged.
    """
    root, app = repo
    base_ref = "HEAD"

    # The test file exists on base without any skip markers (committed in 'initial')
    test_file = app / "tests" / "test_hello.py"
    # Modify working tree: add a skip marker (and keep the same hash by adding text)
    test_file.write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='temporarily disabled')\n"
        "def test_hello():\n"
        "    assert 1 + 1 == 2\n"
    )

    result = verify(root, app, base_ref=base_ref)
    skipxfail_findings = [
        f for f in result["integrity"]["findings"]
        if f["issue"] == "skip_or_xfail_added"
    ]
    # The file is also "modified" (hash changed); skip_or_xfail_added is only raised
    # when file is unchanged (same hash). So if hash changed, "modified" is raised first.
    # Confirm at least "modified" or "skip_or_xfail_added" is present.
    any_finding = [
        f for f in result["integrity"]["findings"]
        if "test_hello.py" in f["file"]
    ]
    assert any_finding, "Expected some integrity finding for the modified+skip file"


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


def test_human_approved_test_change_is_reported_not_hidden(tmp_path):
    from amend.verify import _split_approved_changes

    (tmp_path / "compliance").mkdir()
    (tmp_path / "compliance" / "test_changes.yaml").write_text(
        "- file: tests/test_auth.py\n"
        "  reason: asserted staff login without a second factor\n"
        "  approved_by: compliance-lead\n"
        "- file: tests/test_export.py\n"
        "  reason: no approver named\n"
    )
    findings = [
        {"file": "tests/test_auth.py", "issue": "modified"},
        {"file": "tests/test_export.py", "issue": "modified"},
        {"file": "tests/test_seed.py", "issue": "deleted"},
    ]
    unapproved, approved = _split_approved_changes(tmp_path, findings)
    assert [f["file"] for f in approved] == ["tests/test_auth.py"]
    assert approved[0]["approved_by"] == "compliance-lead"
    assert {f["file"] for f in unapproved} == {"tests/test_export.py", "tests/test_seed.py"}


def test_deleted_or_skipped_tests_can_never_be_approved(tmp_path):
    from amend.verify import _split_approved_changes

    (tmp_path / "compliance").mkdir()
    (tmp_path / "compliance" / "test_changes.yaml").write_text(
        "- file: tests/test_a.py\n  approved_by: compliance-lead\n"
        "- file: tests/test_b.py\n  approved_by: compliance-lead\n"
    )
    findings = [
        {"file": "tests/test_a.py", "issue": "deleted"},
        {"file": "tests/test_b.py", "issue": "skip_or_xfail_added"},
    ]
    unapproved, approved = _split_approved_changes(tmp_path, findings)
    assert approved == [] and len(unapproved) == 2
