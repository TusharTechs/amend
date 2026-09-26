"""Shared pytest fixtures for the Lendwise test suite."""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure demo/lendwise is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import get_db
from app.models import AppSetting, Base


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    connection = db_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()

    # Ensure the encryption key setting exists
    if not session.query(AppSetting).filter(AppSetting.key == "field_encryption_key").first():
        session.add(AppSetting(key="field_encryption_key", value="test-fek"))
        session.commit()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(scope="function")
def client(db_session):
    # Patch logging file handler to avoid creating var/log/ during tests
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="function")
def mfa_token():
    from app.auth import create_token
    return create_token({"sub": "alice@example.com", "customer_id": 1, "mfa": True})


@pytest.fixture(scope="function")
def staff_token():
    from app.auth import create_token
    return create_token({"sub": "ops@lendwise.com", "role": "ops", "mfa": False})
