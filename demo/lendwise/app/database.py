import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Default to var/ next to the app, not the caller's working directory.
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var", "lendwise.db")
os.makedirs(os.path.dirname(_DEFAULT_DB), exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_DB}")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
