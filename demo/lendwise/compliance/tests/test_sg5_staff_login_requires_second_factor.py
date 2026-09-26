"""SG-5 · 16 CFR 314.4(c)(5): MFA for any individual accessing any information system."""
from conftest import DEMO_OTP


def test_staff_login_without_second_factor_is_refused(client):
    resp = client.post("/auth/staff/login", json={"email": "ops@lendwise.com", "password": "staffpass"})
    assert resp.status_code == 401


def test_staff_login_with_second_factor_issues_mfa_session(client):
    from app.auth import decode_token

    resp = client.post("/auth/staff/login",
                       json={"email": "ops@lendwise.com", "password": "staffpass", "otp": DEMO_OTP})
    assert resp.status_code == 200
    assert decode_token(resp.json()["access_token"]).get("mfa") is True
