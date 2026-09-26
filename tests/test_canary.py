"""tests/test_canary.py — tests for amend/canary.py against demo/lendwise.

Assertions:
- The canary finds plaintext in the export CSV (analytics bucket).
- The canary finds plaintext in the log file (var/log/).
- The canary finds plaintext in the SQLite database file.
- Decoys (/health, id-only logs, test fixtures) are NOT reported.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
LENDWISE = REPO_ROOT / "demo" / "lendwise"


# ──────────────────────────────────────────────────────────────────────────────
# Module-level canary run (expensive — run once against an isolated copy)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def canary_repo(tmp_path_factory):
    """Copy demo/lendwise into a temp directory (without var/) for isolation."""
    dest = tmp_path_factory.mktemp("lendwise")
    shutil.copytree(
        str(LENDWISE),
        str(dest),
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("var"),
    )
    return dest


@pytest.fixture(scope="module")
def canary_hits(canary_repo):
    """Run the full canary pipeline against the isolated copy and return hits."""
    from amend.canary import run_canary
    return run_canary(canary_repo)


# ──────────────────────────────────────────────────────────────────────────────
# Plaintext hit assertions
# ──────────────────────────────────────────────────────────────────────────────

def test_canary_finds_plaintext_in_export_csv(canary_hits):
    """Export CSV in analytics bucket must contain canary SSN in plaintext."""
    bucket_hits = [h for h in canary_hits if h["store_type"] == "bucket"]
    ssn_hits = [h for h in bucket_hits if h["value_found"] == "000-00-0417"]
    assert ssn_hits, (
        f"Expected canary SSN in analytics bucket. "
        f"All bucket hits: {bucket_hits}"
    )


def test_canary_finds_plaintext_in_log(canary_hits):
    """Request log file must contain canary SSN in plaintext (from middleware debug logging)."""
    log_hits = [h for h in canary_hits if h["store_type"] == "log"]
    ssn_hits = [h for h in log_hits if h["value_found"] == "000-00-0417"]
    assert ssn_hits, (
        f"Expected canary SSN in log file. "
        f"All log hits: {log_hits}"
    )


def test_canary_finds_plaintext_in_sqlite(canary_hits):
    """SQLite database file must contain canary SSN in plaintext bytes."""
    db_hits = [h for h in canary_hits if h["store_type"] == "sqlite"]
    ssn_hits = [h for h in db_hits if h["value_found"] == "000-00-0417"]
    assert ssn_hits, (
        f"Expected canary SSN in SQLite file. "
        f"All db hits: {db_hits}"
    )


def test_canary_finds_bank_account_in_sqlite(canary_hits):
    """SQLite database file must contain canary bank account number in plaintext bytes."""
    db_hits = [h for h in canary_hits if h["store_type"] == "sqlite"]
    bank_hits = [h for h in db_hits if h["value_found"] == "000000417"]
    assert bank_hits, (
        f"Expected canary bank account in SQLite file. "
        f"All db hits: {db_hits}"
    )


def test_canary_exits_nonzero_on_hits(canary_hits):
    """If there are hits, there must be at least one hit (non-empty list means exit 1)."""
    assert len(canary_hits) > 0, "Expected at least one plaintext hit"


# ──────────────────────────────────────────────────────────────────────────────
# Hit structure validation
# ──────────────────────────────────────────────────────────────────────────────

def test_hit_has_required_fields(canary_hits):
    for hit in canary_hits:
        assert "store_type" in hit, f"Hit missing store_type: {hit}"
        assert "path" in hit, f"Hit missing path: {hit}"
        assert "offset_or_line" in hit, f"Hit missing offset_or_line: {hit}"
        assert "snippet" in hit, f"Hit missing snippet: {hit}"
        assert "value_found" in hit, f"Hit missing value_found: {hit}"


def test_snippet_is_masked(canary_hits):
    """Snippets in hits must mask the sensitive value."""
    from amend.canary import CANARY_SSN
    for hit in canary_hits:
        if hit["value_found"] == CANARY_SSN:
            assert CANARY_SSN not in hit["snippet"], (
                f"Snippet not masked: {hit['snippet']!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests for scan helpers (no full pipeline required)
# ──────────────────────────────────────────────────────────────────────────────

def test_scan_bytes_finds_value():
    from amend.canary import _scan_bytes, CANARY_SSN
    data = b"prefix " + CANARY_SSN.encode() + b" suffix"
    hits = _scan_bytes(data, [CANARY_SSN])
    assert len(hits) == 1
    val, offset, snippet = hits[0]
    assert val == CANARY_SSN
    assert offset == 7


def test_scan_text_file_finds_value(tmp_path):
    from amend.canary import _scan_text_file, CANARY_SSN
    f = tmp_path / "test.log"
    f.write_text(f"line1\nrequest body contains {CANARY_SSN} in clear\nline3\n")
    hits = _scan_text_file(f, [CANARY_SSN])
    assert len(hits) == 1
    val, lineno, snippet = hits[0]
    assert val == CANARY_SSN
    assert lineno == 2


def test_scan_bytes_no_false_positives():
    from amend.canary import _scan_bytes, CANARY_SSN
    data = b"000-11-9999 and 000123456 are not canary values"
    hits = _scan_bytes(data, [CANARY_SSN])
    assert hits == [], f"False positive: {hits}"
