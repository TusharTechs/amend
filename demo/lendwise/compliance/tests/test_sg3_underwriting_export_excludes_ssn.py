"""SG-3 · 16 CFR 314.4(c)(3): the analytics export must not carry SSNs in plaintext,
whatever the column is called."""
import os
import sqlite3

from conftest import CANARY_SSN

VIEWS = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "views.sql")


def test_underwriting_export_contains_no_ssn(tmp_path):
    from jobs.export_underwriting import run

    db = tmp_path / "uw.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, income REAL, status TEXT, ssn TEXT)")
    conn.execute("INSERT INTO applications VALUES (1, 41700.0, 'pending', ?)", (CANARY_SSN,))
    with open(VIEWS) as fh:
        conn.executescript(fh.read())
    conn.commit()
    conn.close()

    out = run(str(db), str(tmp_path / "analytics"))
    content = open(out).read()
    assert CANARY_SSN not in content
    assert "tin" not in content.splitlines()[0].split(",")
