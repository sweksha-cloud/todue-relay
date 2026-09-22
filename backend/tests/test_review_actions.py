"""The four review actions (vote, remove, approve, decline), tested directly and through the dashboard
routes that share them. /approve and /decline had no tests before they were moved into
app/review_actions.py; these pin what they do, including every refusal."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import calendar_client, main, review_actions
from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus
from app.db.session import get_db
from app.review_actions import ActionError
from app.schemas import ExtractionResult


def _row(db, email_id="e1", subject="Nominations due", *, event_id=None, has_deadline=True, action_type="deadline",
         raw="Sep 24 at 5:00 PM", context="ctx", recurring=None):
    deadline = datetime.now(timezone.utc) + timedelta(days=3) if has_deadline else None
    repository.try_claim_email(db, email_id, f"t-{email_id}", subject)
    repository.mark_completed(
        db, email_id,
        ExtractionResult(
            email_id=email_id, event_name=subject, deadline_date_raw=raw, deadline_date=deadline,
            source_context=context, confidence="low", action_type=action_type,
            is_recurring=bool(recurring), recurrence_rule=recurring,
        ),
        calendar_event_id=event_id,
    )
    return db.get(ProcessedEmail, email_id)


def _refused(fn, *args):
    with pytest.raises(ActionError) as e:
        fn(*args)
    return e.value


class TestRecordVote:
    def test_records_the_verdict_and_touches_nothing_else(self, db_session, calendar):
        _row(db_session, event_id="cal-1")

        assert review_actions.record_vote(db_session, "e1", True).user_correction is True
        assert review_actions.record_vote(db_session, "e1", False).user_correction is False
        assert calendar == {"created": [], "deleted": []}

    def test_an_unknown_email_is_a_404(self, db_session, calendar):
        assert _refused(review_actions.record_vote, db_session, "nope", True).status_code == 404


class TestRemoveEvent:
    def test_deletes_the_real_calendar_event_and_hides_the_row(self, db_session, calendar):
        _row(db_session, event_id="cal-1")

        review_actions.remove_event(db_session, "e1")

        assert calendar["deleted"] == ["cal-1"]
        assert repository.count_upcoming_on_calendar(db_session, datetime.now(timezone.utc) - timedelta(days=1)) == 0
        assert "e1" not in [r.email_id for r in repository.list_recent_emails(db_session)]

    def test_refuses_when_there_is_no_live_event_and_calls_nothing(self, db_session, calendar):
        _row(db_session, event_id=None)

        error = _refused(review_actions.remove_event, db_session, "e1")

        assert (error.status_code, error.detail) == (400, "No live Calendar event to remove")
        assert calendar["deleted"] == []

    def test_an_unknown_email_is_a_404(self, db_session, calendar):
        assert _refused(review_actions.remove_event, db_session, "nope").status_code == 404


class TestApprove:
    def test_creates_the_held_back_event_and_records_its_id(self, db_session, calendar):
        row = _row(db_session, subject="Workshop signup", context="Sign up by Friday", event_id=None)
        deadline = row.extraction_deadline_parsed

        approved = review_actions.approve(db_session, "e1")

        assert approved.calendar_event_id == "new-event-id"
        assert len(calendar["created"]) == 1
        sent = calendar["created"][0]
        assert (sent["summary"], sent["description"], sent["deadline"]) == ("Workshop signup", "Sign up by Friday", deadline)
        assert sent["has_time"] is True and sent["recurrence_rule"] is None

    def test_a_date_only_deadline_is_created_without_a_time(self, db_session, calendar):
        _row(db_session, raw="Sep 24", event_id=None)

        review_actions.approve(db_session, "e1")

        assert calendar["created"][0]["has_time"] is False

    def test_a_recurring_item_creates_a_recurring_event(self, db_session, calendar):
        _row(db_session, event_id=None, recurring="RRULE:FREQ=MONTHLY")

        review_actions.approve(db_session, "e1")

        assert calendar["created"][0]["recurrence_rule"] == "RRULE:FREQ=MONTHLY"

    @pytest.mark.parametrize(
        "setup",
        [
            pytest.param(dict(event_id="cal-1"), id="already-on-the-calendar"),
            pytest.param(dict(event_id=None, has_deadline=False, action_type="needs_reply"), id="no-date-to-create-it-on"),
        ],
    )
    def test_refuses_what_cannot_be_approved_and_calls_nothing(self, db_session, calendar, setup):
        _row(db_session, **setup)

        error = _refused(review_actions.approve, db_session, "e1")

        assert (error.status_code, error.detail) == (400, "Not an approvable item")
        assert calendar["created"] == []

    def test_refuses_an_item_that_was_already_declined(self, db_session, calendar):
        _row(db_session, event_id=None)
        review_actions.decline(db_session, "e1")

        assert _refused(review_actions.approve, db_session, "e1").status_code == 400
        assert calendar["created"] == []

    def test_an_unknown_email_is_a_404(self, db_session, calendar):
        assert _refused(review_actions.approve, db_session, "nope").status_code == 404

    def test_approving_twice_does_not_create_a_second_event(self, db_session, calendar):
        _row(db_session, event_id=None)
        review_actions.approve(db_session, "e1")

        assert _refused(review_actions.approve, db_session, "e1").status_code == 400
        assert len(calendar["created"]) == 1


class TestDecline:
    def test_skips_the_item_and_creates_no_event(self, db_session, calendar):
        _row(db_session, event_id=None)

        declined = review_actions.decline(db_session, "e1")

        assert declined.status == ProcessingStatus.SKIPPED
        assert "declined by user" in declined.error_message
        assert calendar == {"created": [], "deleted": []}
        assert repository.count_needs_review(db_session) == 0

    def test_an_unknown_email_is_a_404(self, db_session, calendar):
        assert _refused(review_actions.decline, db_session, "nope").status_code == 404


class TestTheDashboardRoutesStillBehaveTheSame:
    """main.py's routes now delegate to review_actions; these pin the HTTP behaviour that must not change."""

    @pytest.fixture
    def client(self, db_session):
        main.app.dependency_overrides[get_db] = lambda: db_session
        yield TestClient(main.app)
        main.app.dependency_overrides.clear()

    def test_approve_creates_the_event_and_asks_the_page_to_reload(self, client, db_session, calendar):
        _row(db_session, subject="Workshop signup", event_id=None)

        response = client.post("/emails/e1/approve")

        assert response.status_code == 200
        assert response.headers["HX-Refresh"] == "true"  # the email moves section, so the page reloads
        assert db_session.get(ProcessedEmail, "e1").calendar_event_id == "new-event-id"
        assert len(calendar["created"]) == 1

    def test_approve_refuses_with_400_and_an_unknown_email_with_404(self, client, db_session, calendar):
        _row(db_session, event_id="cal-1")

        assert client.post("/emails/e1/approve").status_code == 400
        assert client.post("/emails/nope/approve").status_code == 404
        assert calendar["created"] == []

    def test_decline_skips_the_item_and_returns_the_row(self, client, db_session, calendar):
        _row(db_session, event_id=None)

        response = client.post("/emails/e1/decline")

        assert response.status_code == 200
        assert db_session.get(ProcessedEmail, "e1").status == ProcessingStatus.SKIPPED

    def test_decline_of_an_unknown_email_is_a_404(self, client, calendar):
        assert client.post("/emails/nope/decline").status_code == 404

    def test_correct_and_remove_of_an_unknown_email_are_404(self, client, calendar):
        assert client.post("/emails/nope/correct", data={"is_correct": "true"}).status_code == 404
        assert client.post("/emails/nope/remove").status_code == 404
