"""amend/canary.py — dynamic canary sweep for a FastAPI + SQLAlchemy repo.

Inserts a synthetic canary record through the app's own ORM, fires a real
HTTP request via FastAPI TestClient, runs nightly jobs, then scans all
persistent stores (SQLite file bytes, var/log/**, var/buckets/**) for
plaintext canary values.

No language model required.

Public API
----------
run_canary(repo_root, json_out=None) -> list[dict]
    Returns a list of hit dicts:
        {store_type, path, offset_or_line, snippet, value_found}
    Exits with code 1 when any plaintext hit is found.

CLI: amend canary [--repo REPO] [--json FILE]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# Canary values (fixed synthetic — obviously not real PII)
# ──────────────────────────────────────────────────────────────────────────────

CANARY_SSN = "000-00-0417"
CANARY_BANK = "000000417"
CANARY_VALUES = [CANARY_SSN, CANARY_BANK]

_MASK_RE = re.compile(r"(\d{3})-(\d{2})-(\d{4})")


def _mask(value: str, snippet: str) -> str:
    """Mask middle digits in displayed snippet."""
    return snippet.replace(value, value[:3] + "-**-****" if "-" in value else value[:3] + "***")


# ──────────────────────────────────────────────────────────────────────────────
# Child-process worker script (runs inside the target repo, fully isolated)
# ──────────────────────────────────────────────────────────────────────────────

# This script is executed in a subprocess with cwd=repo_root and explicit env.
# It NEVER imports into the parent process.
_WORKER_SCRIPT = textwrap.dedent("""\
    import json, os, subprocess, sys, importlib.util, logging, shutil, sqlite3

    repo_root = sys.argv[1]
    ssn = sys.argv[2]
    bank = sys.argv[3]

    # ── 1. Reset database (inline — avoids relying on reset.sh finding the right Python) ──
    var_dir = os.path.join(repo_root, "var")
    # Wipe and recreate var/
    if os.path.exists(var_dir):
        shutil.rmtree(var_dir)
    os.makedirs(os.path.join(var_dir, "log"), exist_ok=True)
    os.makedirs(os.path.join(var_dir, "buckets", "analytics"), exist_ok=True)

    # Seed the database using the same Python interpreter
    seed_py = os.path.join(repo_root, "scripts", "seed.py")
    r = subprocess.run(
        [sys.executable, seed_py],
        cwd=repo_root,
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    if r.returncode != 0:
        print(json.dumps({"error": f"seed.py failed: {r.stderr}"}))
        sys.exit(1)

    # Apply SQL views
    views_sql = os.path.join(repo_root, "sql", "views.sql")
    if os.path.exists(views_sql):
        db_file = os.path.join(repo_root, "var", "lendwise.db")
        conn = sqlite3.connect(db_file)
        with open(views_sql) as f:
            conn.executescript(f.read())
        conn.commit()
        conn.close()

    # ── 2. Insert canary via ORM ─────────────────────────────────────────────
    sys.path.insert(0, repo_root)
    db_url = "sqlite:///" + os.path.join(repo_root, "var", "lendwise.db")
    os.environ["DATABASE_URL"] = db_url

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Application, Base, Customer

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    customer = Customer(name="Canary Testperson", email="canary.417@example.com")
    session.add(customer)
    session.flush()

    app_rec = Application(
        customer_id=customer.id,
        ssn=ssn,
        bank_account_number=bank,
        income=41700.0,
        status="pending",
    )
    session.add(app_rec)
    session.commit()
    customer_id = customer.id
    session.close()

    # ── 3. Trigger request logging via TestClient ────────────────────────────
    # Ensure DEBUG so RequestBodyLoggingMiddleware logs request bodies
    os.environ["LOG_LEVEL"] = "DEBUG"

    # Reset logging so fresh basicConfig picks up DEBUG + FileHandler
    root_log = logging.getLogger()
    for h in list(root_log.handlers):
        root_log.removeHandler(h)
        h.close()
    root_log.setLevel(logging.DEBUG)

    # Clear cached app modules so fresh import re-runs logging setup
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("app."):
            del sys.modules[mod_name]

    from fastapi.testclient import TestClient
    from app.auth import create_token
    from app.main import app

    token = create_token({"sub": "canary@example.com", "customer_id": customer_id, "mfa": True})
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/v1/applications/",
            json={
                "customer_id": customer_id,
                "ssn": ssn,
                "bank_account_number": bank,
                "income": 41700.0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    # ── 4. Run jobs ──────────────────────────────────────────────────────────
    db_path = os.path.join(repo_root, "var", "lendwise.db")
    analytics_dir = os.path.join(repo_root, "var", "buckets", "analytics")

    for mod_name in list(sys.modules.keys()):
        if "export_underwriting" in mod_name or "retention" in mod_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        "export_underwriting",
        os.path.join(repo_root, "jobs", "export_underwriting.py"),
    )
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(db_path, analytics_dir)

    spec2 = importlib.util.spec_from_file_location(
        "retention",
        os.path.join(repo_root, "jobs", "retention.py"),
    )
    if spec2 and spec2.loader:
        mod2 = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mod2)
        mod2.run("sqlite:///" + db_path)

    print(json.dumps({"ok": True, "customer_id": customer_id}))
""")


# ──────────────────────────────────────────────────────────────────────────────
# Scan functions (run in parent — only touches files, no imports from target app)
# ──────────────────────────────────────────────────────────────────────────────


def _scan_bytes(data: bytes, canary_values: list[str]) -> list[tuple[str, int, str]]:
    """Return [(value_found, offset, masked_snippet), ...] for each hit in raw bytes."""
    hits = []
    for val in canary_values:
        val_bytes = val.encode()
        offset = 0
        while True:
            idx = data.find(val_bytes, offset)
            if idx == -1:
                break
            start = max(0, idx - 20)
            end = min(len(data), idx + len(val_bytes) + 20)
            snippet = data[start:end].decode(errors="replace")
            masked = _mask(val, snippet)
            hits.append((val, idx, masked))
            offset = idx + 1
    return hits


def _scan_text_file(path: Path, canary_values: list[str]) -> list[tuple[str, int, str]]:
    """Return [(value_found, line_num, masked_snippet), ...] for text file."""
    hits = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return hits
    for lineno, line in enumerate(lines, 1):
        for val in canary_values:
            if val in line:
                masked = _mask(val, line.strip())
                hits.append((val, lineno, masked))
    return hits


def _scan_sqlite(db_path: Path, canary_values: list[str]) -> list[dict]:
    """Scan raw SQLite file bytes for canary values."""
    hits = []
    try:
        data = db_path.read_bytes()
    except Exception:
        return hits
    for val, offset, snippet in _scan_bytes(data, canary_values):
        hits.append(
            {
                "store_type": "sqlite",
                "path": str(db_path),
                "offset_or_line": offset,
                "snippet": snippet,
                "value_found": val,
            }
        )
    return hits


def _scan_directory(root: Path, canary_values: list[str], store_type: str) -> list[dict]:
    """Recursively scan all files under *root* for canary values."""
    hits = []
    if not root.exists():
        return hits
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in (".log", ".csv", ".txt", ".json", ".yaml", ".yml", ""):
            for val, lineno, snippet in _scan_text_file(path, canary_values):
                hits.append(
                    {
                        "store_type": store_type,
                        "path": str(path),
                        "offset_or_line": lineno,
                        "snippet": snippet,
                        "value_found": val,
                    }
                )
        else:
            try:
                data = path.read_bytes()
            except Exception:
                continue
            for val, offset, snippet in _scan_bytes(data, canary_values):
                hits.append(
                    {
                        "store_type": store_type,
                        "path": str(path),
                        "offset_or_line": offset,
                        "snippet": snippet,
                        "value_found": val,
                    }
                )
    return hits


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────


def run_canary(repo_root: Path | str, json_out: str | None = None) -> list[dict]:
    """Full canary pipeline; returns list of hit dicts.

    The reset/insert/request/jobs steps run in a child process with cwd set to
    repo_root and an explicit environment (DATABASE_URL, LOG_LEVEL=DEBUG).
    The parent process only scans files and reads the child's JSON output —
    it never imports the target app and never mutates sys.modules, sys.path,
    os.environ, or the working directory.
    """
    # Resolve to absolute path first so reset.sh is found regardless of cwd
    repo_root = Path(repo_root).resolve()

    db_url = f"sqlite:///{repo_root / 'var' / 'lendwise.db'}"
    child_env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "LOG_LEVEL": "DEBUG",
        "PYTHONPATH": str(repo_root),
    }

    result = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT, str(repo_root), CANARY_SSN, CANARY_BANK],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        env=child_env,
    )

    # Parse the last JSON line from stdout
    stdout_lines = [l for l in result.stdout.strip().splitlines() if l.strip().startswith("{")]
    if stdout_lines:
        payload = json.loads(stdout_lines[-1])
        if "error" in payload:
            raise RuntimeError(f"Canary child process error: {payload['error']}")
    if result.returncode != 0:
        raise RuntimeError(
            f"Canary child process failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    # ── Scan (parent only — pure file I/O) ────────────────────────────────────
    all_hits: list[dict] = []
    db_path = repo_root / "var" / "lendwise.db"
    all_hits.extend(_scan_sqlite(db_path, CANARY_VALUES))
    all_hits.extend(_scan_directory(repo_root / "var" / "log", CANARY_VALUES, "log"))
    all_hits.extend(_scan_directory(repo_root / "var" / "buckets", CANARY_VALUES, "bucket"))

    if json_out:
        out_path = Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(all_hits, fh, indent=2)

    return all_hits


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def cmd_canary(args) -> None:
    repo_root = Path(args.repo)
    json_out = getattr(args, "json", None)

    hits = run_canary(repo_root, json_out)

    if hits:
        print(f"CANARY: {len(hits)} plaintext hit(s) found:")
        for h in hits:
            print(
                f"  [{h['store_type']}] {h['path']}:{h['offset_or_line']}"
                f"  value={h['value_found']!r}  snippet={h['snippet']!r}"
            )
        sys.exit(1)
    else:
        print("CANARY: no plaintext hits found.")
