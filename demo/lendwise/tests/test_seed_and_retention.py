"""Tests for seed script and retention job."""
import datetime
import os
import sys
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_engine(path):
    url = f"sqlite:///{path}"
    return create_engine(url, connect_args={"check_same_thread": False})


def test_seed_creates_customers():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db_url = f"sqlite:///{db_path}"
        # Point the seed at a temp db
        original = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_url
        try:
            # Re-import with fresh engine
            import importlib
            import app.database as db_mod
            saved_engine, saved_session = db_mod.engine, db_mod.SessionLocal
            db_mod.engine = create_engine(db_url, connect_args={"check_same_thread": False})
            db_mod.SessionLocal = sessionmaker(bind=db_mod.engine)

            import scripts.seed as seed_mod
            importlib.reload(seed_mod)
            seed_mod.run()

            from app.models import Customer
            Session = sessionmaker(bind=db_mod.engine)
            session = Session()
            count = session.query(Customer).count()
            session.close()
            assert count == 600
        finally:
            db_mod.engine.dispose()
            db_mod.engine, db_mod.SessionLocal = saved_engine, saved_session
            if original is None:
                del os.environ["DATABASE_URL"]
            else:
                os.environ["DATABASE_URL"] = original


def test_retention_marks_old_customers():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = f"sqlite:///{tmpdir}/retention_test.db"
        engine = create_engine(db_url, connect_args={"check_same_thread": False})

        from app.models import Base, Customer
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        old_date = datetime.datetime.utcnow() - datetime.timedelta(days=8 * 365)
        recent_date = datetime.datetime.utcnow() - datetime.timedelta(days=30)

        old_customer = Customer(name="Old User", email="old@example.com", last_activity_at=old_date)
        recent_customer = Customer(name="Recent User", email="recent@example.com", last_activity_at=recent_date)
        session.add_all([old_customer, recent_customer])
        session.commit()
        session.close()

        from jobs.retention import run
        count = run(db_url)
        assert count == 1

        session = Session()
        old = session.query(Customer).filter(Customer.email == "old@example.com").first()
        recent = session.query(Customer).filter(Customer.email == "recent@example.com").first()
        assert old.deleted_at is not None
        assert recent.deleted_at is None
        session.close()
        engine.dispose()
