"""The add-on's summary endpoint against the real test Postgres. Authentication is bypassed here
(test_addon_auth.py covers it); this is about what the endpoint says."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.addon_api import available_actions, deadline_text
from app.addon_app import app
from app.addon_auth import require_owner
from app import date_utils
from app.date_utils import has_explicit_time
from app.db import repository
from app.db.models import ProcessedEmail, RunStatus
from app.db.session import get_db
from app.schemas import ExtractionResult

URL = "/api/addon/summary"


@pytest.fixture(autouse=True)
def pacific_time(monkeypatch):
    """Deadline wording depends on the configured zone. Pin it, so the tests pass on a machine or CI
    runner with a different one (CI has no .env and would otherwise fall back to UTC)."""
    monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_owner] = lambda: "owner@example.com"
    yield TestClient(app)
    app.dependency_overrides.clear()


def _row(db, email_id, subject, *, event_id="cal-1", days=3, raw="in 3 days", action_type="deadline", confidence="high"):
    """One finished extraction. event_id=None means nothing went on the calendar."""
    deadline = datetime.now(timezone.utc) + timedelta(days=days) if action_type == "deadline" else None
    repository.try_claim_email(db, email_id, f"t-{email_id}", subject)
    repository.mark_completed(
        db,
        email_id,
        ExtractionResult(
            email_id=email_id, event_name=subject, deadline_date_raw=raw, deadline_date=deadline,
            source_context="ctx", confidence=confidence, action_type=action_type,
        ),
        calendar_event_id=event_id,
    )


def _ids(items):
    return [i["email_id"] for i in items]


class TestAnEmptyDatabase:
    def test_reports_zeros_and_no_run_instead_of_failing(self, client):
        body = client.get(URL).json()

        assert body["counts"] == {"needs_review": 0, "upcoming": 0, "action_items": 0}
        assert body["needs_review"] == body["upcoming"] == body["action_items"] == []
        assert body["latest_run"] is None


class TestNeedsReview:
    def test_a_completed_extraction_with_no_calendar_event_needs_review(self, client, db_session):
        _row(db_session, "r1", "Workshop signup", event_id=None, confidence="low")

        body = client.get(URL).json()

        assert body["counts"]["needs_review"] == 1
        assert _ids(body["needs_review"]) == ["r1"]
        assert body["needs_review"][0]["status"] == "needs review"
        assert body["needs_review"][0]["on_calendar"] is False
        assert body["needs_review"][0]["confidence"] == "low"

    def test_something_already_on_the_calendar_does_not_need_review(self, client, db_session):
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        assert client.get(URL).json()["counts"]["needs_review"] == 0

    def test_a_declined_item_no_longer_needs_review(self, client, db_session):
        _row(db_session, "r1", "Workshop signup", event_id=None, confidence="low")
        repository.mark_skipped(db_session, "r1", "declined by user")

        assert client.get(URL).json()["counts"]["needs_review"] == 0

    def test_an_old_item_is_not_pushed_out_by_a_flood_of_newer_ones(self, client, db_session):
        _row(db_session, "old", "The old one", event_id=None)
        for i in range(15):
            _row(db_session, f"n{i}", f"Newer {i}", event_id=None)

        body = client.get(URL).json()

        assert body["counts"]["needs_review"] == 16  # the true total, not just the 10 shown
        assert len(body["needs_review"]) == 10

    def test_newest_first(self, client, db_session):
        _row(db_session, "a", "First", event_id=None)
        _row(db_session, "b", "Second", event_id=None)

        assert _ids(client.get(URL).json()["needs_review"]) == ["b", "a"]


class TestUpcoming:
    def test_calendar_events_are_listed_soonest_first(self, client, db_session):
        _row(db_session, "later", "Later", days=5)
        _row(db_session, "sooner", "Sooner", days=1)

        body = client.get(URL).json()

        assert _ids(body["upcoming"]) == ["sooner", "later"]
        assert body["counts"]["upcoming"] == 2
        assert all(item["on_calendar"] for item in body["upcoming"])
        assert body["upcoming"][0]["status"] == "added"

    def test_an_event_whose_deadline_passed_days_ago_is_no_longer_upcoming(self, client, db_session):
        _row(db_session, "past", "Long gone", days=-5)
        _row(db_session, "today", "Earlier today", days=0)

        assert _ids(client.get(URL).json()["upcoming"]) == ["today"]  # inside the one-day grace

    def test_a_removed_event_is_not_listed(self, client, db_session):
        _row(db_session, "gone", "Removed", days=2)
        repository.remove_calendar_event(db_session, "gone")

        body = client.get(URL).json()

        assert body["upcoming"] == [] and body["counts"]["upcoming"] == 0


class TestActionItems:
    def test_dateless_items_are_listed_separately_from_deadlines(self, client, db_session):
        _row(db_session, "a1", "Reply to advisor", event_id=None, action_type="needs_reply")
        _row(db_session, "c1", "Rent due")

        body = client.get(URL).json()

        assert _ids(body["action_items"]) == ["a1"]
        assert body["counts"]["action_items"] == 1
        assert body["action_items"][0]["deadline_text"] is None
        assert body["action_items"][0]["action_type"] == "needs_reply"
        assert "a1" not in _ids(body["needs_review"])


class TestVotesAndFlags:
    def test_an_explicit_vote_is_reported(self, client, db_session):
        _row(db_session, "ok", "Right", days=1)
        _row(db_session, "bad", "Wrong", days=2)
        _row(db_session, "none", "Unreviewed", days=3)
        repository.set_correction(db_session, "ok", True)
        repository.set_correction(db_session, "bad", False)

        votes = {i["email_id"]: i["vote"] for i in client.get(URL).json()["upcoming"]}

        assert votes == {"ok": "correct", "bad": "incorrect", "none": None}


class TestLatestRun:
    def test_reports_how_the_last_run_went(self, client, db_session):
        run = repository.start_run(db_session)
        repository.finish_run(db_session, run.id, status=RunStatus.SUCCESS, emails_fetched=24, emails_processed=3, emails_failed=1)

        latest = client.get(URL).json()["latest_run"]

        assert (latest["status"], latest["fetched"], latest["processed"], latest["failed"]) == ("success", 24, 3, 1)
        assert latest["started_text"]


class TestDeadlineWording:
    """The API words deadlines so the add-on does not have to: Apps Script is the hard place to test."""

    def test_a_date_only_deadline_shows_no_time(self):
        assert has_explicit_time("Sep 24") is False
        assert deadline_text(datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc), False) == "Thu Sep 24"

    def test_a_timed_deadline_shows_the_local_time(self):
        assert has_explicit_time("Sep 24 at 5:00 PM") is True
        # 00:00 UTC on Sep 25 is 5:00 PM on Sep 24 in Pacific time (the configured zone)
        assert deadline_text(datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc), True) == "Thu Sep 24, 5:00 PM"

    def test_midnight_and_noon_use_a_twelve_hour_clock(self):
        assert deadline_text(datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc), True) == "Thu Sep 24, 12:00 PM"
        assert deadline_text(datetime(2026, 9, 24, 7, 30, tzinfo=timezone.utc), True) == "Thu Sep 24, 12:30 AM"

    def test_no_deadline_gives_no_text(self):
        assert deadline_text(None, None) is None


class TestWhichActionsAreOffered:
    """The API decides what the card may offer, so the add-on only draws the buttons it is told about."""

    def _actions(self, db, email_id):
        return available_actions(db.get(ProcessedEmail, email_id))

    def test_an_item_needing_review_can_be_added_or_declined(self, db_session):
        _row(db_session, "r1", "Workshop", event_id=None, confidence="low")

        assert self._actions(db_session, "r1") == ["approve", "decline"]

    def test_an_item_with_no_date_can_only_be_declined_because_there_is_nothing_to_create(self, db_session):
        repository.try_claim_email(db_session, "nodate", "t", "Vague")
        repository.mark_completed(
            db_session, "nodate",
            ExtractionResult(email_id="nodate", event_name="Vague", deadline_date_raw=None, deadline_date=None,
                             source_context="c", confidence="low", action_type="deadline"),
            calendar_event_id=None,
        )

        assert self._actions(db_session, "nodate") == ["decline"]

    def test_an_event_on_the_calendar_can_be_voted_on_and_removed(self, db_session):
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        assert self._actions(db_session, "c1") == ["vote_correct", "vote_incorrect", "remove"]

    def test_once_a_verdict_is_given_only_remove_is_left(self, db_session):
        _row(db_session, "c1", "Rent due", event_id="cal-1")
        repository.set_correction(db_session, "c1", False)

        assert self._actions(db_session, "c1") == ["remove"]

    def test_action_items_have_no_actions_from_the_card_yet(self, db_session):
        _row(db_session, "a1", "Reply to advisor", event_id=None, action_type="needs_reply")

        assert self._actions(db_session, "a1") == []

    def test_a_declined_item_offers_nothing(self, db_session):
        _row(db_session, "r1", "Workshop", event_id=None)
        repository.mark_skipped(db_session, "r1", "declined by user")

        assert self._actions(db_session, "r1") == []

    def test_the_summary_carries_them(self, client, db_session):
        _row(db_session, "r1", "Workshop", event_id=None)
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        body = client.get(URL).json()

        assert body["needs_review"][0]["actions"] == ["approve", "decline"]
        assert body["upcoming"][0]["actions"] == ["vote_correct", "vote_incorrect", "remove"]


class TestTheActionEndpoints:
    def _post(self, client, email_id, action, **kwargs):
        return client.post(f"/api/addon/emails/{email_id}/{action}", **kwargs)

    def test_approving_creates_the_event_and_says_so(self, client, db_session, calendar):
        _row(db_session, "r1", "Workshop signup", event_id=None)

        response = self._post(client, "r1", "approve")

        body = response.json()
        assert response.status_code == 200 and body["ok"] is True
        assert body["message"] == "Added to your calendar"
        assert body["email"]["on_calendar"] is True and body["email"]["status"] == "added"
        assert len(calendar["created"]) == 1

    def test_declining_skips_it_without_touching_the_calendar(self, client, db_session, calendar):
        _row(db_session, "r1", "Workshop", event_id=None)

        response = self._post(client, "r1", "decline")

        assert response.json()["message"] == "Won't add this"
        assert response.json()["email"]["status"] == "skipped"
        assert calendar == {"created": [], "deleted": []}

    def test_removing_deletes_the_real_event(self, client, db_session, calendar):
        _row(db_session, "c1", "Rent due", event_id="cal-9")

        response = self._post(client, "c1", "remove")

        assert response.json()["message"] == "Removed from your calendar"
        assert calendar["deleted"] == ["cal-9"]
        assert client.get(URL).json()["counts"]["upcoming"] == 0

    @pytest.mark.parametrize("verdict,message,expected", [("correct", "Marked correct", "correct"), ("incorrect", "Marked incorrect", "incorrect")])
    def test_voting_records_the_verdict_and_never_touches_the_calendar(self, client, db_session, calendar, verdict, message, expected):
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        response = self._post(client, "c1", "vote", json={"vote": verdict})

        assert response.json()["message"] == message
        assert response.json()["email"]["vote"] == expected
        assert response.json()["email"]["actions"] == ["remove"]
        assert calendar == {"created": [], "deleted": []}

    def test_a_vote_must_be_correct_or_incorrect(self, client, db_session, calendar):
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        assert self._post(client, "c1", "vote", json={"vote": "maybe"}).status_code == 422
        assert self._post(client, "c1", "vote", json={}).status_code == 422

    def test_a_refused_action_says_why_and_changes_nothing(self, client, db_session, calendar):
        _row(db_session, "c1", "Rent due", event_id="cal-1")

        response = self._post(client, "c1", "approve")  # already on the calendar

        assert response.status_code == 400 and response.json()["detail"] == "Not an approvable item"
        assert calendar["created"] == []

    @pytest.mark.parametrize("action", ["approve", "decline", "remove"])
    def test_an_unknown_email_is_a_404(self, client, action, calendar):
        assert self._post(client, "nope", action).status_code == 404

    def test_an_unknown_email_cannot_be_voted_on(self, client, calendar):
        assert self._post(client, "nope", "vote", json={"vote": "correct"}).status_code == 404

    def test_the_same_action_twice_does_not_act_twice(self, client, db_session, calendar):
        _row(db_session, "r1", "Workshop", event_id=None)
        self._post(client, "r1", "approve")

        assert self._post(client, "r1", "approve").status_code == 400
        assert len(calendar["created"]) == 1
