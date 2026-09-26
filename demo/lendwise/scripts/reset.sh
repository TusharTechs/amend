#!/usr/bin/env bash
# Wipe var/ and rebuild the database with fresh seed data (see reset.py, which works on any OS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the repository's .venv (three levels above scripts/: scripts/ -> demo/lendwise -> demo -> repo root).
VENV_PYTHON="$SCRIPT_DIR/../../../.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

exec "$PYTHON" "$SCRIPT_DIR/reset.py"
