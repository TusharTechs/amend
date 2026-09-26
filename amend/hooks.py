"""amend/hooks.py — Bob lifecycle hooks for the Amend workflow.

Invoked by Bob as:
  python -m amend.hooks pre-tool-use
  python -m amend.hooks post-tool-use

Bob writes the hook payload JSON to stdin.

Exit codes:
  0 — allow
  2 — block (pre-tool-use only); reason on stderr
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Patterns that must be blocked in pre-tool-use
# ---------------------------------------------------------------------------

# Dangerous shell commands
_BLOCKED_CMD_RE = re.compile(
    r"(?:"
    r"rm\s+-rf"
    r"|git\s+push\s+--force"
    r"|git\s+reset\s+--hard"
    r"|DROP\s+TABLE"
    r")",
    re.IGNORECASE,
)

# Locked paths (always relative to the workspace / repo root)
_LOCKED_PATH_RES = [
    re.compile(r"^evidence/"),
    re.compile(r"^benchmark/"),
    re.compile(r"^\.bob/"),
]


def _is_locked_test(path: str, repo_root: Path, base_ref: str) -> bool:
    """Return True if *path* resolves to a test file that exists at *base_ref*."""
    import subprocess

    if not path:
        return False
    # Normalise to relative path from repo root
    try:
        rel = str(Path(path).resolve().relative_to(repo_root))
    except ValueError:
        rel = path

    # Must be under tests/
    if not rel.startswith("tests/"):
        return False

    # Check if the file exists at base_ref in git
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}:{rel}"],
        cwd=str(repo_root),
        capture_output=True,
    )
    return result.returncode == 0


def _is_locked_path(path: str) -> bool:
    """Return True if path matches a locked directory."""
    if not path:
        return False
    # Normalise to forward slashes, strip leading ./
    norm = path.replace("\\", "/").lstrip("./")
    return any(pat.match(norm) for pat in _LOCKED_PATH_RES)


def _find_repo_root(cwd: str) -> Path:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path(cwd)


def _load_base_ref(repo_root: Path) -> str:
    cfg_path = repo_root / "compliance" / "amend.yaml"
    if cfg_path.exists():
        try:
            import yaml  # type: ignore[import]
            with open(cfg_path) as fh:
                cfg = yaml.safe_load(fh) or {}
            return cfg.get("base_ref", "main")
        except Exception:
            pass
    return "main"


def _events_path(repo_root: Path) -> Path:
    d = repo_root / ".amend"
    d.mkdir(exist_ok=True)
    return d / "events.ndjson"


def _append_event(repo_root: Path, event: str, summary: str) -> None:
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": event,
        "summary": summary,
    }
    try:
        with open(_events_path(repo_root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pre-tool-use handler
# ---------------------------------------------------------------------------

def _pre_tool_use(payload: dict) -> None:
    cwd = payload.get("cwd", ".")
    repo_root = _find_repo_root(cwd)
    base_ref = _load_base_ref(repo_root)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # 1. Check shell commands
    if tool_name in ("execute_command", "run_command"):
        cmd = tool_input.get("command", "")
        if _BLOCKED_CMD_RE.search(cmd):
            sys.stderr.write(f"amend-hooks: blocked dangerous command: {cmd!r}\n")
            sys.exit(2)

    # 2. Check file edit paths
    edit_tools = {
        "write_file", "apply_diff", "search_and_replace",
        "insert_content", "create_file", "edit_file",
    }
    if tool_name in edit_tools:
        path = tool_input.get("path", "")

        # Check locked directories
        if _is_locked_path(path):
            sys.stderr.write(
                f"amend-hooks: edits to {path!r} are locked (evidence/, benchmark/, .bob/)\n"
            )
            sys.exit(2)

        # Check locked test files (exist at base_ref under tests/)
        if _is_locked_test(path, repo_root, base_ref):
            sys.stderr.write(
                f"amend-hooks: {path!r} is a locked test file (exists at {base_ref})\n"
            )
            sys.exit(2)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Post-tool-use handler
# ---------------------------------------------------------------------------

def _post_tool_use(payload: dict) -> None:
    cwd = payload.get("cwd", ".")
    repo_root = _find_repo_root(cwd)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    _append_event(repo_root, "post-tool-use", f"tool={tool_name}")

    # After editing a .py file, run guard and print findings for that file
    edit_tools = {
        "write_file", "apply_diff", "search_and_replace",
        "insert_content", "create_file", "edit_file",
    }
    if tool_name in edit_tools:
        path = tool_input.get("path", "")
        if path.endswith(".py"):
            try:
                from amend.guard import run_guard
                import yaml  # type: ignore[import]

                cfg_path = repo_root / "compliance" / "amend.yaml"
                cfg: dict = {}
                if cfg_path.exists():
                    with open(cfg_path) as fh:
                        cfg = yaml.safe_load(fh) or {}

                failures = run_guard(repo_root, cfg)
                # Normalise path for comparison
                try:
                    rel = str(Path(path).resolve().relative_to(repo_root))
                except ValueError:
                    rel = path

                file_failures = [f for f in failures if f.file == rel or f.file == path]
                for f in file_failures:
                    sys.stdout.write(f"guard: {f}\n")
            except Exception as exc:
                sys.stdout.write(f"guard: error running guard: {exc}\n")

    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python -m amend.hooks pre-tool-use|post-tool-use\n")
        sys.exit(1)

    event = sys.argv[1]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    if event == "pre-tool-use":
        _pre_tool_use(payload)
    elif event == "post-tool-use":
        _post_tool_use(payload)
    else:
        sys.stderr.write(f"amend-hooks: unknown event {event!r}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
