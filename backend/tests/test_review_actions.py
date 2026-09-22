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


class TestApproveAt:
    """"Reschedule" on an item needing review: add it to the calendar at a time the person chooses."""

    def _chosen(self):
        return datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)

    def test_creates_the_event_at_the_chosen_time_and_records_it(self, db_session, calendar):
        _row(db_session, subject="Workshop signup", context="Sign up by Friday", event_id=None)

        row = review_actions.approve_at(db_session, "e1", self._chosen())

        sent = calendar["created"][0]
        assert sent["deadline"] == self._chosen() and sent["has_time"] is True
        assert (sent["summary"], sent["description"]) == ("Workshop signup", "Sign up by Friday")
        assert row.calendar_event_id == "new-event-id"
        assert row.extraction_deadline_parsed == self._chosen() and row.extraction_has_time is True

    def test_it_can_add_an_item_that_had_no_date_at_all_which_approve_cannot(self, db_session, calendar):
        _row(db_session, event_id=None, has_deadline=False)
        assert _refused(review_actions.approve, db_session, "e1").status_code == 400  # nothing to approve

        review_actions.approve_at(db_session, "e1", self._chosen())

        assert db_session.get(ProcessedEmail, "e1").calendar_event_id == "new-event-id"

    def test_choosing_a_time_clears_the_implausible_date_warning(self, db_session, calendar):
        _row(db_session, event_id=None)
        row = db_session.get(ProcessedEmail, "e1")
        row.is_implausible_date = True
        db_session.commit()

        review_actions.approve_at(db_session, "e1", self._chosen())

        assert db_session.get(ProcessedEmail, "e1").is_implausible_date is False

    def test_it_is_a_one_off_even_if_a_recurrence_was_extracted_for_another_date(self, db_session, calendar):
        _row(db_session, event_id=None, recurring="RRULE:FREQ=MONTHLY")

        review_actions.approve_at(db_session, "e1", self._chosen())

        assert not calendar["created"][0].get("recurrence_rule")

    def test_it_records_no_verdict(self, db_session, calendar):
        _row(db_session, event_id=None)

        assert review_actions.approve_at(db_session, "e1", self._chosen()).user_correction is None

    def test_the_item_moves_from_needs_review_to_to_check(self, db_session, calendar):
        _row(db_session, event_id=None)
        assert repository.count_category(db_session, "needs_review") == 1

        review_actions.approve_at(db_session, "e1", self._chosen())

        assert repository.count_category(db_session, "needs_review") == 0
        assert repository.count_category(db_session, "to_check") == 1

    @pytest.mark.parametrize(
        "setup",
        [
            pytest.param(dict(event_id="cal-1"), id="already-on-the-calendar"),
            pytest.param(dict(event_id=None, has_deadline=False, action_type="needs_reply"), id="an-action-item-has-its-own-schedule"),
        ],
    )
    def test_refuses_what_is_not_a_held_back_deadline_and_calls_nothing(self, db_session, calendar, setup):
        _row(db_session, **setup)

        error = _refused(review_actions.approve_at, db_session, "e1", self._chosen())

        assert (error.status_code, error.detail) == (400, "Not a held-back item")
        assert calendar["created"] == []

    def test_refuses_an_item_already_denied_or_failed(self, db_session, calendar):
        _row(db_session, "denied", event_id=None)
        review_actions.decline(db_session, "denied")
        repository.try_claim_email(db_session, "broke", "t", "s")
        repository.mark_failed(db_session, "broke", "ValueError: bad")

        assert _refused(review_actions.approve_at, db_session, "denied", self._chosen()).status_code == 400
        assert _refused(review_actions.approve_at, db_session, "broke", self._chosen()).status_code == 400
        assert calendar["created"] == []

    def test_an_unknown_email_is_a_404(self, db_session, calendar):
        assert _refused(review_actions.approve_at, db_session, "nope", self._chosen()).status_code == 404


class TestTrashEmail:
    def test_trashes_the_source_message_and_hides_the_row(self, db_session, calendar, gmail):
        _row(db_session, "e1", event_id="cal-1")

        row = review_actions.trash_email(db_session, "e1")

        assert gmail["trashed"] == ["e1"]
        assert row.error_message == repository.TRASHED_MESSAGE
        assert repository.list_recent_emails(db_session) == []

    def test_leaves_a_live_calendar_event_untouched(self, db_session, calendar, gmail):
        _row(db_session, "e1", event_id="cal-1")

        row = review_actions.trash_email(db_session, "e1")

        assert row.calendar_event_id == "cal-1"  # unlike Remove, this never touches the event
        assert calendar["deleted"] == []

    def test_leaves_an_existing_vote_untouched(self, db_session, calendar, gmail):
        _row(db_session, "e1", event_id="cal-1")
        review_actions.record_vote(db_session, "e1", True)

        row = review_actions.trash_email(db_session, "e1")

        assert row.user_correction is True

    def test_works_on_an_action_item_with_no_calendar_event_at_all(self, db_session, calendar, gmail):
        _row(db_session, "a1", event_id=None, has_deadline=False, action_type="needs_reply")

        row = review_actions.trash_email(db_session, "a1")

        assert gmail["trashed"] == ["a1"]
        assert repository.count_action_items(db_session) == 0

    def test_an_unknown_email_is_a_404_and_gmail_is_never_called(self, db_session, gmail):
        assert _refused(review_actions.trash_email, db_session, "nope").status_code == 404
        assert gmail["trashed"] == []
