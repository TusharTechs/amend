"""Tests for the underwriting export job."""
import csv
import datetime
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _setup_db_with_view(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY,
            income REAL,
            status TEXT,
            ssn TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO applications (id, income, status, ssn) VALUES (1, 55000.0, 'approved', '000-12-3456')"
    )
    # Apply the view
    sql_path = os.path.join(os.path.dirname(__file__), "..", "sql", "views.sql")
    with open(sql_path) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def test_export_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _setup_db_with_view(db_path)

        output_dir = os.path.join(tmpdir, "analytics")
        from jobs.export_underwriting import run
        out_path = run(db_path, output_dir)

        assert os.path.exists(out_path)
        date_str = datetime.date.today().isoformat()
        assert f"uw_export_{date_str}.csv" in out_path


def test_export_csv_contents():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _setup_db_with_view(db_path)

        output_dir = os.path.join(tmpdir, "analytics")
        from jobs.export_underwriting import run
        out_path = run(db_path, output_dir)

        with open(out_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        assert rows[0]["id"] == "1"
        assert rows[0]["income"] == "55000.0"
        assert rows[0]["status"] == "approved"
        assert "tin" not in rows[0]
        assert "000-12-3456" not in open(out_path).read()
