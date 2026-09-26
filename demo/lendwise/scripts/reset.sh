#!/usr/bin/env bash
# Wipe var/ and rebuild the database with fresh seed data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

# Prefer the repository's .venv (three levels above scripts/: scripts/ -> demo/lendwise -> demo -> repo root).
VENV_PYTHON="$SCRIPT_DIR/../../../.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

cd "$ROOT"

echo "Removing var/..."
rm -rf var

echo "Recreating directories..."
mkdir -p var/log var/buckets/analytics

echo "Seeding database..."
"$PYTHON" scripts/seed.py

echo "Applying SQL views..."
"$PYTHON" - <<'PYEOF'
import sqlite3, os
conn = sqlite3.connect("var/lendwise.db")
with open("sql/views.sql") as f:
    conn.executescript(f.read())
conn.commit()
conn.close()
print("views applied")
PYEOF

echo "Done."
