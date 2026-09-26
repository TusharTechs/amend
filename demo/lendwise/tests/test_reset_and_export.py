"""End-to-end test: reset.py → export_underwriting.py → CSV in analytics bucket."""
import datetime
import os
import subprocess
import sys

import pytest

# Ensure demo/lendwise is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LENDWISE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(LENDWISE_ROOT, "..", ".."))
RESET_PY = os.path.join(LENDWISE_ROOT, "scripts", "reset.py")
ANALYTICS_DIR = os.path.join(LENDWISE_ROOT, "var", "buckets", "analytics")


@pytest.mark.skipif(
    not os.path.isfile(RESET_PY),
    reason="reset.py not found",
)
def test_reset_and_export_creates_csv(tmp_path, monkeypatch):
    """Run reset.py then export_underwriting.py; assert CSV exists in analytics bucket."""
    result = subprocess.run(
        [sys.executable, RESET_PY],
        cwd=LENDWISE_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"reset.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Verify database was created
    db_path = os.path.join(LENDWISE_ROOT, "var", "lendwise.db")
    assert os.path.exists(db_path), "var/lendwise.db was not created by reset.py"

    # Run export job directly (import path requires demo/lendwise in sys.path)
    from jobs.export_underwriting import run as export_run

    out_path = export_run(db_path, ANALYTICS_DIR)

    # Assert the CSV exists in var/buckets/analytics
    assert os.path.exists(out_path), f"Export CSV not found at {out_path}"
    assert os.path.dirname(os.path.abspath(out_path)) == os.path.abspath(ANALYTICS_DIR), (
        "CSV was not written inside var/buckets/analytics"
    )

    date_str = datetime.date.today().isoformat()
    assert f"uw_export_{date_str}.csv" in os.path.basename(out_path)

    # Assert the file is non-empty (has header + at least one data row)
    with open(out_path) as fh:
        lines = fh.readlines()
    assert len(lines) >= 2, "CSV has no data rows — export appears empty"
