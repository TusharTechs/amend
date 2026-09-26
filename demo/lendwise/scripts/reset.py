"""Wipe var/ and rebuild the Lendwise database with fresh seed data.

Works on macOS, Linux and Windows:  python scripts/reset.py
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    print("Removing var/...")
    shutil.rmtree(ROOT / "var", ignore_errors=True)
    print("Recreating directories...")
    (ROOT / "var" / "log").mkdir(parents=True, exist_ok=True)
    (ROOT / "var" / "buckets" / "analytics").mkdir(parents=True, exist_ok=True)
    print("Seeding database...")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py")], cwd=ROOT, check=True)
    print("Applying SQL views...")
    conn = sqlite3.connect(ROOT / "var" / "lendwise.db")
    try:
        conn.executescript((ROOT / "sql" / "views.sql").read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
