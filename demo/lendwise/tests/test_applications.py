"""Tests for application creation and retrieval."""
import json
import os

import pytest


FIXTURES_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_applicants.json")


def test_create_application(client, mfa_token, db_session):
    from app.models import Customer
    customer = Customer(name="Test User", email="testuser@example.com")
    db_session.add(customer)
    db_session.commit()

    resp = client.post(
        "/api/v1/applications/",
        json={
            "customer_id": customer.id,
            "ssn": "000-11-2222",
            "bank_account_number": "0009876543",
            "income": 60000.0,
        },
        headers={"Authorization": f"Bearer {mfa_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["customer_id"] == customer.id
    assert data["status"] == "pending"
    assert "id" in data


def test_get_application(client, mfa_token, db_session):
    from app.models import Application, Customer
    customer = Customer(name="Jane Doe", email="janedoe@example.com")
    db_session.add(customer)
    db_session.commit()

    app = Application(
        customer_id=customer.id,
        ssn="000-22-3333",
        bank_account_number="0001111111",
        income=75000.0,
        status="approved",
    )
    db_session.add(app)
    db_session.commit()

    resp = client.get(
        f"/api/v1/applications/{app.id}",
        headers={"Authorization": f"Bearer {mfa_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == app.id
    assert data["status"] == "approved"
    assert data["income"] == 75000.0


def test_get_application_not_found(client, mfa_token):
    resp = client.get(
        "/api/v1/applications/99999",
        headers={"Authorization": f"Bearer {mfa_token}"},
    )
    assert resp.status_code == 404


def test_sample_applicants_fixture_loads():
    with open(FIXTURES_PATH) as f:
        data = json.load(f)
    assert len(data) == 5
    for record in data:
        assert "ssn" in record
        assert record["ssn"].startswith("000-")
