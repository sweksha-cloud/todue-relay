"""Fixtures for the claim-logic/idempotency tests, which need a real
Postgres — the claim logic uses `sqlalchemy.dialects.postgresql.insert`
(`ON CONFLICT ... WHERE ... RETURNING`), which has no SQLite equivalent.

Local dev: point TEST_DATABASE_URL at a throwaway Postgres (see
README's Docker command). CI: a `postgres:` service container in the
workflow provides one — see .github/workflows/tests.yml.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.models import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:55432/testdb",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(eng)  # clean slate — don't inherit stale local state
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    """One session per test, tables truncated after — tests don't leak
    state into each other regardless of run order.
    """
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
