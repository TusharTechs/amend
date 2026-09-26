"""Tests for health, auth, and token endpoints."""
import pytest


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_customer_login_success(client):
    resp = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "pass1234", "otp": "123456"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_customer_login_bad_password(client):
    resp = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "wrong", "otp": "123456"},
    )
    assert resp.status_code == 401


def test_customer_login_missing_otp(client):
    resp = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "pass1234"},
    )
    assert resp.status_code == 401


def test_staff_login_success(client):
    resp = client.post(
        "/auth/staff/login",
        json={"email": "ops@lendwise.com", "password": "staffpass"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_mobile_token_success(client):
    resp = client.post(
        "/auth/mobile/token",
        json={"email": "alice@example.com", "password": "pass1234"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_protected_route_without_token(client):
    resp = client.get("/api/v1/applications/1")
    assert resp.status_code == 401


def test_protected_route_with_staff_token_no_mfa(client, staff_token):
    resp = client.get(
        "/api/v1/applications/1",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 403
