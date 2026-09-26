"""Retention job.

Marks customers as deleted after 7 years of inactivity, in line with the
data-minimisation schedule defined in the data-governance policy.
"""
import datetime
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Allow running from the project root or from demo/lendwise
sys.path.insert(0, ".")

from app.models import Customer  # noqa: E402


def run(db_url: str = "sqlite:///./var/lendwise.db") -> int:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    db = Session()

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7 * 365)
    affected = (
        db.query(Customer)
        .filter(Customer.last_activity_at < cutoff, Customer.deleted_at.is_(None))
        .all()
    )

    now = datetime.datetime.utcnow()
    for customer in affected:
        customer.deleted_at = now

    db.commit()
    db.close()
    return len(affected)


if __name__ == "__main__":
    count = run()
    print(f"soft-deleted {count} customers")
