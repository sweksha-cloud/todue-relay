"""scripts/retry_failed.py: lists by default, changes something only with --apply."""

from datetime import datetime, timezone

from app.db import repository
from app.db.models import ProcessedEmail
from scripts.retry_failed import run


def _parked(db, email_id, error):
    repository.try_claim_email(db, email_id, "t", f"Subject {email_id}")
    repository.mark_failed(db, email_id, error)
    row = db.get(ProcessedEmail, email_id)
    row.attempt_count = 3
    db.commit()


def test_the_default_only_lists_and_changes_nothing(db_session):
    _parked(db_session, "e1", "ServerError: 503 UNAVAILABLE")

    found = run(db_session, apply=False)

    assert [f[0] for f in found] == ["e1"]
    assert db_session.get(ProcessedEmail, "e1").attempt_count == 3


def test_apply_gives_the_listed_emails_fresh_attempts(db_session):
    _parked(db_session, "e1", "ServerError: 503 UNAVAILABLE")

    found = run(db_session, apply=True)

    assert [f[0] for f in found] == ["e1"]
    assert db_session.get(ProcessedEmail, "e1").attempt_count == 0


def test_it_does_not_touch_an_email_that_failed_for_its_own_reasons(db_session):
    _parked(db_session, "own", "ValueError: bad response")

    assert run(db_session, apply=True) == []
    assert db_session.get(ProcessedEmail, "own").attempt_count == 3


def test_with_nothing_parked_it_reports_nothing(db_session):
    assert run(db_session, apply=True) == []
