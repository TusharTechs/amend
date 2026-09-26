"""Tests for amend/obligations.py using real regulation XML files."""
import pytest
from pathlib import Path

from amend.regdiff import parse, diff
from amend.obligations import Obligation, validate, load

REG_DIR = Path(__file__).parent.parent / "regulations" / "16cfr314"

V2021 = REG_DIR / "2021-01-01.xml"
V2026 = REG_DIR / "2026-09-01.xml"


@pytest.fixture(scope="module")
def v2021():
    return parse(V2021, "2021-01-01")


@pytest.fixture(scope="module")
def v2026():
    return parse(V2026, "2026-09-01")


@pytest.fixture(scope="module")
def redline_2021_2026(v2021, v2026):
    return diff(v2021, v2026)


@pytest.fixture(scope="module")
def versions_both(v2021, v2026):
    return {"2021-01-01": v2021, "2026-09-01": v2026}


# The exact normalized text of 314.4(c)(5) from the 2026 version
MFA_QUOTE = (
    "Implement multi-factor authentication for any individual accessing any information system, "
    "unless your Qualified Individual has approved in writing the use of reasonably equivalent "
    "or more secure access controls;"
)


def _mfa_obligation(**overrides) -> Obligation:
    """Return a valid MFA obligation with optional field overrides."""
    base = dict(
        id="OB-MFA-001",
        citation="314.4(c)(5)",
        version="2026-09-01",
        change="added",
        quote=MFA_QUOTE,
        statement="Implement MFA for all system access",
        shape="code",
        status="proposed",
        exceptions=[],
        approved_by=None,
    )
    base.update(overrides)
    return Obligation(**base)


# ---------------------------------------------------------------------------
# Accepts a correct obligation
# ---------------------------------------------------------------------------

def test_accept_valid_mfa_obligation(versions_both, redline_2021_2026):
    ob = _mfa_obligation()
    problems = validate([ob], versions_both, redline_2021_2026)
    assert problems == [], f"Expected no problems, got: {problems}"


# ---------------------------------------------------------------------------
# Rejects paraphrased quote
# ---------------------------------------------------------------------------

def test_reject_paraphrased_quote(versions_both, redline_2021_2026):
    ob = _mfa_obligation(
        id="OB-MFA-002",
        quote="Implement MFA for all individuals accessing information systems",
    )
    problems = validate([ob], versions_both, redline_2021_2026)
    assert any("quote" in str(p).lower() or "verbatim" in str(p).lower() for p in problems), \
        f"Expected a quote-related problem, got: {problems}"


# ---------------------------------------------------------------------------
# Rejects invented citation
# ---------------------------------------------------------------------------

def test_reject_invented_citation(versions_both, redline_2021_2026):
    ob = _mfa_obligation(
        id="OB-MFA-003",
        citation="314.4(c)(12)",
        quote="Some nonexistent text",
    )
    problems = validate([ob], versions_both, redline_2021_2026)
    assert any("citation" in str(p).lower() or "does not exist" in str(p).lower() for p in problems), \
        f"Expected a citation problem, got: {problems}"


# ---------------------------------------------------------------------------
# Rejects valid quote but wrong version (2021 didn't have c)(5))
# ---------------------------------------------------------------------------

def test_reject_right_quote_wrong_version(versions_both, redline_2021_2026):
    """314.4(c)(5) with MFA text doesn't exist in the 2021 version."""
    ob = _mfa_obligation(
        id="OB-MFA-004",
        version="2021-01-01",
        citation="314.4(c)(5)",
        change="unchanged",
    )
    problems = validate([ob], versions_both, redline_2021_2026)
    # Either the citation doesn't exist in 2021 OR the quote isn't found there
    assert problems, f"Expected problems for right quote in wrong version, got none"


# ---------------------------------------------------------------------------
# Rejects fabricated exception
# ---------------------------------------------------------------------------

def test_reject_fabricated_exception(versions_both, redline_2021_2026):
    ob = _mfa_obligation(
        id="OB-MFA-005",
        exceptions=["This exception text does not appear anywhere in the actual clause text."],
    )
    problems = validate([ob], versions_both, redline_2021_2026)
    assert any("exception" in str(p).lower() for p in problems), \
        f"Expected an exception-related problem, got: {problems}"


# ---------------------------------------------------------------------------
# Rejects approved without approver
# ---------------------------------------------------------------------------

def test_reject_approved_without_approver(versions_both, redline_2021_2026):
    ob = _mfa_obligation(
        id="OB-MFA-006",
        status="approved",
        approved_by=None,
    )
    problems = validate([ob], versions_both, redline_2021_2026)
    assert any("approved_by" in str(p).lower() or "approved" in str(p).lower() for p in problems), \
        f"Expected approved_by problem, got: {problems}"
