"""tests/test_hooks.py — unit tests for amend/hooks.py.

Tests:
  - Editing a locked test file (exists at base_ref under tests/) exits 2.
  - Editing app/auth_routes.py (not a test file) exits 0.
  - A command matching rm -rf exits 2.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOKS_CMD = [sys.executable, "-m", "amend.hooks"]


def _run_hook(event: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        HOOKS_CMD + [event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _pre_tool_use_edit(path: str) -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test",
        "cwd": str(REPO_ROOT),
        "hook_event_name": "PreToolUse",
        "tool_name": "write_file",
        "tool_input": {"path": path},
        "tool_use_id": "test-tool-1",
    }
    return _run_hook("pre-tool-use", payload)


def _pre_tool_use_cmd(command: str) -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test",
        "cwd": str(REPO_ROOT),
        "hook_event_name": "PreToolUse",
        "tool_name": "execute_command",
        "tool_input": {"command": command},
        "tool_use_id": "test-tool-2",
    }
    return _run_hook("pre-tool-use", payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_locked_test_file_exits_2():
    """Editing a test file that exists at base_ref (main) under tests/ must exit 2."""
    # tests/test_verify.py is a real test file committed on main
    result = _pre_tool_use_edit("tests/test_verify.py")
    assert result.returncode == 2, (
        f"Expected exit 2 for locked test file, got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )


def test_app_file_exits_0():
    """Editing app/auth_routes.py (not under tests/) must exit 0."""
    result = _pre_tool_use_edit("demo/lendwise/app/auth_routes.py")
    assert result.returncode == 0, (
        f"Expected exit 0 for app file, got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )


def test_rm_rf_exits_2():
    """A command matching rm -rf must be blocked with exit 2."""
    result = _pre_tool_use_cmd("rm -rf /tmp/something")
    assert result.returncode == 2, (
        f"Expected exit 2 for rm -rf, got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )


def test_git_push_force_exits_2():
    """git push --force must be blocked."""
    result = _pre_tool_use_cmd("git push --force origin main")
    assert result.returncode == 2, (
        f"Expected exit 2 for git push --force, got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )


def test_git_reset_hard_exits_2():
    """git reset --hard must be blocked."""
    result = _pre_tool_use_cmd("git reset --hard HEAD~1")
    assert result.returncode == 2, (
        f"Expected exit 2 for git reset --hard, got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )


def test_safe_command_exits_0():
    """A benign command must exit 0."""
    result = _pre_tool_use_cmd("pytest -q tests/test_guard.py")
    assert result.returncode == 0, (
        f"Expected exit 0 for safe command, got {result.returncode}."
    )


def test_evidence_path_locked():
    """Editing a file under evidence/ must be blocked."""
    result = _pre_tool_use_edit("evidence/run1/matrix.json")
    assert result.returncode == 2


def test_benchmark_path_locked():
    """Editing a file under benchmark/ must be blocked."""
    result = _pre_tool_use_edit("benchmark/results.json")
    assert result.returncode == 2
