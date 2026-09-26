"""tests/test_guard.py — tests for amend/guard.py against demo/lendwise.

Exact findings asserted:
  G-MFA:       GET /internal/v1/support/impersonate/{customer_id}
               GET /api/v1/profile
  G-MFA-LOGIN: POST /auth/staff/login
               POST /auth/mobile/token
  G-LOG:       only the request-body log in app/logging_mw.py
  G-EXPORT:    jobs/export_underwriting.py

No findings for:
  /api/v1/applications/*, /api/v1/customers/{customer_id}, /health,
  POST /auth/login, unmounted /incidents router, id-only log line.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
LENDWISE = REPO_ROOT / "demo" / "lendwise"

sys.path.insert(0, str(LENDWISE))


@pytest.fixture(scope="module")
def guard_failures():
    from amend.guard import run_guard
    return run_guard(LENDWISE)


# ──────────────────────────────────────────────────────────────────────────────
# G-MFA: exact routes
# ──────────────────────────────────────────────────────────────────────────────

def test_g_mfa_flags_impersonate(guard_failures):
    """G-MFA must flag GET /internal/v1/support/impersonate/{customer_id} (mounted sub-app)."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    impersonate = [
        f for f in mfa_fails
        if "impersonate" in f.message
    ]
    assert impersonate, (
        f"Expected G-MFA for impersonate route. G-MFA failures: {[str(f) for f in mfa_fails]}"
    )


def test_g_mfa_flags_profile(guard_failures):
    """G-MFA must flag GET /api/v1/profile (mobile_router, only get_current_user, no require_mfa)."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    profile = [f for f in mfa_fails if "/profile" in f.message]
    assert profile, (
        f"Expected G-MFA for /profile route. G-MFA failures: {[str(f) for f in mfa_fails]}"
    )


def test_g_mfa_cites_correct_clause(guard_failures):
    for f in guard_failures:
        if f.rule == "G-MFA":
            assert "314.4(c)(5)" in f.clause, f"Wrong clause for G-MFA: {f.clause}"


def test_g_mfa_not_flagged_applications(guard_failures):
    """Routes under /api/v1/applications/* must NOT be flagged by G-MFA (protected by require_mfa)."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    app_fails = [f for f in mfa_fails if "/api/v1/applications" in f.message]
    assert not app_fails, f"/api/v1/applications flagged by G-MFA: {app_fails}"


def test_g_mfa_not_flagged_customers(guard_failures):
    """Routes under /api/v1/customers/* must NOT be flagged by G-MFA."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    cust_fails = [f for f in mfa_fails if "/customers/" in f.message]
    assert not cust_fails, f"/api/v1/customers flagged by G-MFA: {cust_fails}"


def test_g_mfa_not_flagged_health(guard_failures):
    """/health must NOT be flagged by G-MFA."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    health_fails = [f for f in mfa_fails if "/health" in f.message]
    assert not health_fails, f"/health flagged by G-MFA: {health_fails}"


def test_g_mfa_not_flagged_auth_login(guard_failures):
    """POST /auth/login (which does verify OTP) must NOT be flagged by G-MFA."""
    mfa_fails = [f for f in guard_failures if f.rule == "G-MFA"]
    login_fails = [f for f in mfa_fails if "/auth/login" in f.message]
    assert not login_fails, f"POST /auth/login flagged by G-MFA: {login_fails}"


# ──────────────────────────────────────────────────────────────────────────────
# G-MFA-LOGIN: exact routes
# ──────────────────────────────────────────────────────────────────────────────

def test_g_mfa_login_flags_staff_login(guard_failures):
    """G-MFA-LOGIN must flag POST /auth/staff/login (no MFA check)."""
    login_fails = [f for f in guard_failures if f.rule == "G-MFA-LOGIN"]
    staff = [f for f in login_fails if "staff/login" in f.message]
    assert staff, (
        f"Expected G-MFA-LOGIN for staff/login. G-MFA-LOGIN failures: {[str(f) for f in login_fails]}"
    )


def test_g_mfa_login_flags_mobile_token(guard_failures):
    """G-MFA-LOGIN must flag POST /auth/mobile/token (no MFA check)."""
    login_fails = [f for f in guard_failures if f.rule == "G-MFA-LOGIN"]
    mobile = [f for f in login_fails if "mobile/token" in f.message]
    assert mobile, (
        f"Expected G-MFA-LOGIN for mobile/token. G-MFA-LOGIN failures: {[str(f) for f in login_fails]}"
    )


def test_g_mfa_login_not_flagged_auth_login(guard_failures):
    """POST /auth/login (with OTP check) must NOT be flagged by G-MFA-LOGIN."""
    login_fails = [f for f in guard_failures if f.rule == "G-MFA-LOGIN"]
    # /auth/login path — but not /staff/login or /mobile/token
    plain_login = [
        f for f in login_fails
        if f.message.rstrip().endswith("/auth/login issues a token/session without verifying a second factor (314.4(c)(5))")
        or "/auth/login" in f.message and "staff" not in f.message and "mobile" not in f.message
    ]
    assert not plain_login, f"POST /auth/login flagged by G-MFA-LOGIN: {plain_login}"


def test_g_mfa_login_cites_correct_clause(guard_failures):
    for f in guard_failures:
        if f.rule == "G-MFA-LOGIN":
            assert "314.4(c)(5)" in f.clause, f"Wrong clause for G-MFA-LOGIN: {f.clause}"


# ──────────────────────────────────────────────────────────────────────────────
# G-LOG: only the request-body log in app/logging_mw.py
# ──────────────────────────────────────────────────────────────────────────────

def test_g_log_flags_request_body_in_logging_mw(guard_failures):
    """G-LOG must flag the debug log in logging_mw.py that logs the request body."""
    log_fails = [f for f in guard_failures if f.rule == "G-LOG"]
    body_fails = [f for f in log_fails if "logging_mw" in f.file or "body" in f.message.lower()]
    assert body_fails, (
        f"Expected G-LOG for request-body log in logging_mw.py. "
        f"G-LOG failures: {[str(f) for f in log_fails]}"
    )


def test_g_log_not_flagged_id_only_log(guard_failures):
    """The 'application created id=...' log line (id only) must NOT trigger G-LOG."""
    log_fails = [f for f in guard_failures if f.rule == "G-LOG"]
    id_fails = [f for f in log_fails if "application created id" in f.message.lower()]
    assert not id_fails, f"id-only log incorrectly flagged: {id_fails}"


def test_g_log_not_flagged_request_completed(guard_failures):
    """The 'request completed method=... status=... duration_ms=...' log must NOT trigger G-LOG."""
    log_fails = [f for f in guard_failures if f.rule == "G-LOG"]
    # The info log in logging_mw.py only has method/path/status/duration args
    completed_fails = [
        f for f in log_fails
        if "logging_mw" in f.file and "duration_ms" in f.message
    ]
    assert not completed_fails, f"request-completed log incorrectly flagged: {completed_fails}"


def test_g_log_cites_correct_clause(guard_failures):
    for f in guard_failures:
        if f.rule == "G-LOG":
            assert "314.4(c)(3)" in f.clause, f"Wrong clause for G-LOG: {f.clause}"


# ──────────────────────────────────────────────────────────────────────────────
# G-EXPORT: export_underwriting.py
# ──────────────────────────────────────────────────────────────────────────────

def test_g_export_flags_underwriting_job(guard_failures):
    """G-EXPORT must flag jobs/export_underwriting.py."""
    export_fails = [f for f in guard_failures if f.rule == "G-EXPORT"]
    uw_fails = [f for f in export_fails if "export_underwriting" in f.file]
    assert uw_fails, (
        f"Expected G-EXPORT for export_underwriting.py. "
        f"G-EXPORT failures: {[str(f) for f in export_fails]}"
    )


def test_g_export_cites_correct_clauses(guard_failures):
    for f in guard_failures:
        if f.rule == "G-EXPORT":
            assert "314.4(c)(3)" in f.clause
            assert "314.4(c)(6)(i)" in f.clause


# ──────────────────────────────────────────────────────────────────────────────
# Unmounted /incidents router — must not appear as a finding
# ──────────────────────────────────────────────────────────────────────────────

def test_incidents_router_not_flagged(guard_failures):
    """The /incidents router is never included in the app — must not produce any findings."""
    incidents_fails = [f for f in guard_failures if "incident" in f.message.lower()]
    assert not incidents_fails, f"/incidents router flagged: {incidents_fails}"


# ──────────────────────────────────────────────────────────────────────────────
# Failure str format
# ──────────────────────────────────────────────────────────────────────────────

def test_failure_str_format(guard_failures):
    """Each failure's str() must follow 'file:line  rule  clause  message'."""
    for f in guard_failures:
        s = str(f)
        parts = s.split("  ", 3)
        assert len(parts) == 4, f"Failure str format wrong: {s!r}"
        assert ":" in parts[0], f"No file:line in: {parts[0]!r}"
        assert parts[1] in ("G-MFA", "G-MFA-LOGIN", "G-LOG", "G-EXPORT"), f"Unknown rule: {parts[1]!r}"
        assert "314.4" in parts[2], f"No CFR citation in clause: {parts[2]!r}"
