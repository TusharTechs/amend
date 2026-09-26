"""Tests for amend/regdiff.py using real regulation XML files."""
import pytest
from pathlib import Path

from amend.regdiff import parse, diff, Version

REG_DIR = Path(__file__).parent.parent / "regulations" / "16cfr314"

V2021 = REG_DIR / "2021-01-01.xml"
V2023 = REG_DIR / "2023-07-01.xml"
V2026 = REG_DIR / "2026-09-01.xml"


@pytest.fixture(scope="module")
def v2021() -> Version:
    return parse(V2021, "2021-01-01")


@pytest.fixture(scope="module")
def v2026() -> Version:
    return parse(V2026, "2026-09-01")


@pytest.fixture(scope="module")
def v2023() -> Version:
    return parse(V2023, "2023-07-01")


# ---------------------------------------------------------------------------
# Word count
# ---------------------------------------------------------------------------

def test_2021_word_count_under_900(v2021):
    assert v2021.word_count() < 900, f"Expected <900 words, got {v2021.word_count()}"


def test_2026_word_count_over_5000(v2026):
    assert v2026.word_count() > 5000, f"Expected >5000 words, got {v2026.word_count()}"


# ---------------------------------------------------------------------------
# Specific citations exist
# ---------------------------------------------------------------------------

def test_314_4_c_5_text(v2026):
    text = v2026.clauses.get("314.4(c)(5)", "")
    assert text.startswith(
        "Implement multi-factor authentication for any individual accessing any information system"
    ), f"Unexpected text: {text[:100]}"


def test_required_citations_exist(v2026):
    for cit in [
        "314.4(c)(3)",
        "314.4(c)(6)(i)",
        "314.4(c)(8)",
        "314.4(j)(1)",
        "314.2(m)",
    ]:
        assert cit in v2026.clauses, f"Missing citation: {cit}"


def test_314_4_i_exists(v2026):
    assert "314.4(i)" in v2026.clauses, "314.4(i) should exist"


def test_314_4_h_7_i_does_not_exist(v2026):
    assert "314.4(h)(7)(i)" not in v2026.clauses, "314.4(h)(7)(i) should not exist"


def test_314_4_j_1_sub_clauses(v2026):
    for sub in ["(i)", "(ii)", "(iii)", "(iv)", "(v)", "(vi)"]:
        cit = f"314.4(j)(1){sub}"
        assert cit in v2026.clauses, f"Missing citation: {cit}"


# ---------------------------------------------------------------------------
# Diff: 2023-07-01 → 2026-09-01
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def diff_2023_to_2026(v2023, v2026):
    return diff(v2023, v2026)


def test_diff_2023_314_2_m_added(diff_2023_to_2026):
    """314.2(m) changes from Penetration testing to Notification event."""
    entries_by_cit = {e.citation: e for e in diff_2023_to_2026}
    assert "314.2(m)" in entries_by_cit, "314.2(m) should appear in diff"
    e = entries_by_cit["314.2(m)"]
    # It may be added or modified depending on whether penetration testing moved
    assert e.change in {"added", "modified"}, f"Expected added or modified, got {e.change}"


def test_diff_2023_314_4_j_added(diff_2023_to_2026):
    entries_by_cit = {e.citation: e for e in diff_2023_to_2026}
    assert "314.4(j)" in entries_by_cit
    e = entries_by_cit["314.4(j)"]
    assert e.change == "added", f"314.4(j) should be added, got {e.change}"


def test_diff_2023_314_4_c_modified(diff_2023_to_2026):
    entries_by_cit = {e.citation: e for e in diff_2023_to_2026}
    assert "314.4(c)" in entries_by_cit
    e = entries_by_cit["314.4(c)"]
    assert e.change == "modified", f"314.4(c) should be modified, got {e.change}"


def test_diff_2023_at_least_30_renumbered(diff_2023_to_2026):
    renumbered = [e for e in diff_2023_to_2026 if e.change == "renumbered"]
    assert len(renumbered) >= 30, f"Expected ≥30 renumbered, got {len(renumbered)}"


def test_diff_2023_no_substantive_for_example(diff_2023_to_2026):
    substantive_types = {"added", "removed", "modified"}
    bad = [
        e for e in diff_2023_to_2026
        if e.change in substantive_types
        and (e.new_text or e.old_text or "").strip() == "For example:"
    ]
    assert bad == [], f"Found substantive 'For example:' entries: {bad}"


# ---------------------------------------------------------------------------
# Diff: 2021-01-01 → 2026-09-01
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def diff_2021_to_2026(v2021, v2026):
    return diff(v2021, v2026)


def test_diff_2021_c3_c5_c6i_c8_added(diff_2021_to_2026):
    added = {e.citation for e in diff_2021_to_2026 if e.change == "added"}
    for cit in ["314.4(c)(3)", "314.4(c)(5)", "314.4(c)(6)(i)", "314.4(c)(8)"]:
        assert cit in added, f"{cit} should be added in 2021→2026 diff"
