"""Nightly underwriting export job.

Reads v_uw_export and writes a CSV to the analytics bucket for downstream
scoring models. The export carries no government identifiers
(16 CFR 314.4(c)(3)); the bucket expires objects after two years
(314.4(c)(6)(i), see infra/buckets.tf).
"""
import csv
import datetime
import os
import sqlite3
import sys


def run(db_path: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    out_path = os.path.join(output_dir, f"uw_export_{date_str}.csv")

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT id, income, status FROM v_uw_export")
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
    finally:
        conn.close()

    with open(out_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows(rows)

    return out_path


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "var/lendwise.db"
    dest = sys.argv[2] if len(sys.argv) > 2 else "var/buckets/analytics"
    path = run(db, dest)
    print(f"exported {path}")
