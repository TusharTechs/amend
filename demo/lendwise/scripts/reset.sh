#!/usr/bin/env bash
# Wipe var/ and rebuild the database with fresh seed data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

cd "$ROOT"

echo "Removing var/..."
rm -rf var

echo "Recreating directories..."
mkdir -p var/log var/buckets/analytics

echo "Seeding database..."
python scripts/seed.py

echo "Applying SQL views..."
python - <<'PYEOF'
import sqlite3, os
conn = sqlite3.connect("var/lendwise.db")
with open("sql/views.sql") as f:
    conn.executescript(f.read())
conn.commit()
conn.close()
print("views applied")
PYEOF

echo "Done."
