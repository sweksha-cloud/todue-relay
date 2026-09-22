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


@pytest.fixture
def calendar(monkeypatch):
    """A fake Google Calendar that records what would have been sent to it, so a test can check both
    that an action reached the calendar and that a refused one did not."""
    from app import calendar_client

    calls = {"created": [], "deleted": []}

    def fake_create(service, **kwargs):
        calls["created"].append(kwargs)
        return "new-event-id"

    monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(calendar_client, "create_event", fake_create)
    monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    return calls


@pytest.fixture
def gmail(monkeypatch):
    """A fake Gmail that records what would have been trashed, so a test can check both that an
    action reached Gmail and that a refused one did not."""
    from app import gmail_client

    calls = {"trashed": []}
    monkeypatch.setattr(gmail_client, "get_gmail_service", lambda: object())
    monkeypatch.setattr(gmail_client, "trash_message", lambda service, message_id: calls["trashed"].append(message_id))
    return calls
