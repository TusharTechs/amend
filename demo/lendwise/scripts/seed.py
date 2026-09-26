"""Seed script – deterministic synthetic data (no real PII).

Uses random.Random(42) for reproducibility. Generates 600 customers with
plausible-sounding names drawn from a short fixed list. SSNs use area number 000
and bank account numbers start with 000 to make it obvious they are synthetic.
"""
import datetime
import os
import random
import sys

# Allow running as `python scripts/seed.py` from demo/lendwise/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal, engine
from app.models import AppSetting, Application, AuditEvent, Base, Customer

FIRST_NAMES = [
    "Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Hector",
    "Iris", "James", "Karen", "Liam", "Mia", "Noah", "Olivia", "Paul",
    "Quinn", "Rosa", "Sam", "Tina", "Uma", "Victor", "Wendy", "Xander",
    "Yara", "Zack",
]

LAST_NAMES = [
    "Adams", "Baker", "Clark", "Davis", "Evans", "Foster", "Garcia",
    "Harris", "Ingram", "Jones", "King", "Lewis", "Moore", "Nelson",
    "Owen", "Parker", "Quinn", "Reed", "Scott", "Taylor", "Upton",
    "Vance", "Walker", "Xavier", "Young", "Zhang",
]

RNG = random.Random(42)


def _ssn(rng: random.Random) -> str:
    group = rng.randint(10, 99)
    serial = rng.randint(1000, 9999)
    return f"000-{group:02d}-{serial}"


def _bank_account(rng: random.Random) -> str:
    suffix = rng.randint(100000, 999999)
    return f"000{suffix}"


def _random_date(rng: random.Random, start: datetime.datetime, end: datetime.datetime) -> datetime.datetime:
    delta = end - start
    return start + datetime.timedelta(seconds=rng.randint(0, int(delta.total_seconds())))


def run():
    os.makedirs("var", exist_ok=True)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    # App settings
    if not db.query(AppSetting).filter(AppSetting.key == "field_encryption_key").first():
        db.add(AppSetting(key="field_encryption_key", value="lendwise-dev-fek-change-in-prod"))
    db.commit()

    # Customers + Applications
    now = datetime.datetime(2024, 6, 1)
    start = datetime.datetime(2015, 1, 1)

    for i in range(600):
        first = RNG.choice(FIRST_NAMES)
        last = RNG.choice(LAST_NAMES)
        email = f"{first.lower()}.{last.lower()}.{i}@example.com"

        last_activity = _random_date(RNG, start, now)

        customer = Customer(
            name=f"{first} {last}",
            email=email,
            last_activity_at=last_activity,
        )
        db.add(customer)
        db.flush()

        # ~60% of customers have an application
        if RNG.random() < 0.6:
            app = Application(
                customer_id=customer.id,
                ssn=_ssn(RNG),
                bank_account_number=_bank_account(RNG),
                income=round(RNG.uniform(28000, 150000), 2),
                status=RNG.choice(["pending", "approved", "declined"]),
                created_at=_random_date(RNG, last_activity, now),
            )
            db.add(app)

    db.commit()
    db.close()
    print("seed complete")


if __name__ == "__main__":
    run()
