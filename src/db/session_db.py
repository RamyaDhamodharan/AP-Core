"""
Application module for session db.
Flow: database session setup for API and worker calls.
Called by: scripts/backfill_invoices.py, scripts/create_admin.py,
scripts/evaluate.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured in .env")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    """
    Return get db.
    Inputs: no explicit inputs.
    Output: the value or None returned by its body.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
