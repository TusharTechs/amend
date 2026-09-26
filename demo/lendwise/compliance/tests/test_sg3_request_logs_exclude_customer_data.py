"""SG-3 · 16 CFR 314.4(c)(3): customer information must not land in logs in plaintext."""
import logging

from conftest import CANARY_ACCOUNT, CANARY_SSN


def test_request_logging_never_writes_ssn_or_account_number(client, token, caplog):
    caplog.set_level(logging.DEBUG, logger="lendwise.http")
    client.post(
        "/api/v1/applications/",
        json={"customer_id": 1, "ssn": CANARY_SSN, "bank_account_number": CANARY_ACCOUNT, "income": 41700.0},
        headers={"Authorization": f"Bearer {token(mfa=True)}"},
    )
    assert CANARY_SSN not in caplog.text
    assert CANARY_ACCOUNT not in caplog.text
