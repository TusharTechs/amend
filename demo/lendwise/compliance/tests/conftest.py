"""Fixtures for obligation tests. They use only names that already exist at
the base commit, so the same tests run unchanged before and after the fix."""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import get_db  # noqa: E402
from app.models import Base  # noqa: E402

DEMO_OTP = "123456"
CANARY_SSN = "000-00-0417"
CANARY_ACCOUNT = "000000417"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db_session):
    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def token():
    from app.auth import create_token

    def make(**claims):
        return create_token({"sub": "alice@example.com", "customer_id": 1, **claims})
    return make
