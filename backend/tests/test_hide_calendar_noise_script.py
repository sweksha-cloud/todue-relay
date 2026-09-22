"""scripts/hide_calendar_noise.py: re-checks each row live against a (faked) Gmail service, using
the real detection function, and only acts with --apply. No real Gmail or Calendar calls."""

import pytest

from app import calendar_client
from app.db import repository
from app.db.models import ProcessedEmail
from app.schemas import ExtractionResult
from scripts.hide_calendar_noise import HIDDEN_MESSAGE, find_candidates, hide


class _FakeMessages:
    def __init__(self, by_id: dict):
        self._by_id = by_id

    def get(self, userId, id, format):  # noqa: A002 - matches the real Gmail API's kwarg name
        return self

    def execute(self):
        raise NotImplementedError  # only reached via the outer .get(...).execute() chain below


class _FakeService:
    """service.users().messages().get(id=...).execute() -> a canned message dict, by email_id."""

    def __init__(self, by_id: dict):
        self._by_id = by_id

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format):  # noqa: A002
        self._current = id
        return self

    def execute(self):
        return self._by_id[self._current]


def _msg(headers: list[dict], has_ics: bool = False) -> dict:
    payload = {"headers": headers}
    if has_ics:
        payload["mimeType"] = "text/calendar"
        payload["filename"] = "invite.ics"
    return {"payload": payload}


def _row(db, email_id, subject="Some subject", event_id=None, error=None):
    repository.try_claim_email(db, email_id, "t", subject)
    repository.mark_completed(
        db, email_id,
        ExtractionResult(email_id=email_id, event_name=subject, deadline_date_raw="in 3 days",
                         deadline_date=None, source_context="c", confidence="high", action_type="deadline"),
        calendar_event_id=event_id,
    )
    if error:
        row = db.get(ProcessedEmail, email_id)
        row.error_message = error
        db.commit()


@pytest.fixture
def calendar(monkeypatch):
    calls = {"deleted": []}
    monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    return calls


class TestFindCandidates:
    def test_a_calendar_generated_row_is_found(self, db_session):
        _row(db_session, "e1", subject="New event: Something")
        service = _FakeService({"e1": _msg([{"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"}])})

        found = find_candidates(db_session, service)

        assert [r.email_id for r in found] == ["e1"]

    def test_an_ordinary_row_is_not_found(self, db_session):
        _row(db_session, "e1", subject="Assignment due Friday")
        service = _FakeService({"e1": _msg([{"name": "From", "value": "professor@school.edu"}])})

        assert find_candidates(db_session, service) == []

    def test_an_already_hidden_row_is_skipped_even_though_gmail_would_say_yes(self, db_session):
        """Real proof of the skip, not an accident of the fake data: the message IS calendar-
        generated (the service would say so if asked), so this only stays out of the result if
        the already-hidden check runs before ever consulting Gmail."""
        _row(db_session, "e1", subject="New event: Something", error=repository.REMOVED_BY_USER_MESSAGE)
        service = _FakeService({"e1": _msg([{"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"}])})

        assert find_candidates(db_session, service) == []

    def test_this_scripts_own_earlier_message_is_also_treated_as_already_hidden(self, db_session):
        _row(db_session, "e1", subject="New event: Something", error=HIDDEN_MESSAGE)
        service = _FakeService({"e1": _msg([{"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"}])})

        assert find_candidates(db_session, service) == []

    def test_a_lookup_failure_for_one_row_does_not_stop_the_rest(self, db_session):
        _row(db_session, "e1")
        _row(db_session, "e2", subject="New event: Something")
        service = _FakeService({"e2": _msg([{"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"}])})
        # e1 is missing from the fake service's data, so looking it up raises KeyError

        found = find_candidates(db_session, service)

        assert [r.email_id for r in found] == ["e2"]


class TestHide:
    def test_a_row_with_a_live_event_gets_it_deleted_and_is_marked_removed(self, db_session, calendar):
        _row(db_session, "e1", event_id="cal-1")

        action = hide(db_session, db_session.get(ProcessedEmail, "e1"))

        assert calendar["deleted"] == ["cal-1"]
        row = db_session.get(ProcessedEmail, "e1")
        assert row.calendar_event_id is None
        assert row.error_message == repository.REMOVED_BY_USER_MESSAGE
        assert "deleted its Calendar event" in action

    def test_a_row_with_no_event_is_just_hidden(self, db_session, calendar):
        _row(db_session, "e1", event_id=None)

        action = hide(db_session, db_session.get(ProcessedEmail, "e1"))

        assert calendar["deleted"] == []  # nothing to delete
        assert db_session.get(ProcessedEmail, "e1").error_message == HIDDEN_MESSAGE
        assert "hidden" in action
