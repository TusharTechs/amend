"""SG-5 · 16 CFR 314.4(c)(5): the mobile token path is an access path too."""


def test_mobile_token_without_second_factor_is_refused(client):
    resp = client.post("/auth/mobile/token", json={"email": "alice@example.com", "password": "pass1234"})
    assert resp.status_code == 401


def test_mobile_profile_rejects_session_without_mfa(client, token):
    resp = client.get("/api/v1/profile", headers={"Authorization": f"Bearer {token()}"})
    assert resp.status_code == 403
