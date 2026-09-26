"""SG-5 · 16 CFR 314.4(c)(5): the legacy support console must not bypass MFA."""
from app.auth import STAFF_API_KEY


def test_support_console_refuses_staff_key_without_mfa_session(client):
    resp = client.get("/internal/v1/support/impersonate/1", headers={"X-Staff-Key": STAFF_API_KEY})
    assert resp.status_code in (401, 403)
