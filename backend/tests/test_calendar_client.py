from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from app.calendar_client import build_event_body, create_event, delete_event, update_event


def _http_error(status: int) -> HttpError:
    return HttpError(resp=SimpleNamespace(status=status, reason="x"), content=b"{}")


class TestBuildEventBody:
    def test_timed_event_gets_explicit_utc_offset(self):
        """Regression test for a real created-event bug: Google treated a
        naive dateTime as UTC regardless of a separate timeZone field
        (16:45 sent -> stored as 09:45-07:00). The fix attaches the offset
        directly to the dateTime string so there's nothing for the API to
        misinterpret.
        """
        with patch("app.calendar_client.detect_local_timezone", return_value="America/Los_Angeles"):
            body = build_event_body("Test", "desc", datetime(2026, 9, 11, 16, 45), has_time=True)

        assert body["start"]["dateTime"] == "2026-09-11T16:45:00-07:00"
        assert body["start"]["timeZone"] == "America/Los_Angeles"

    def test_already_aware_deadline_is_not_double_converted(self):
        from zoneinfo import ZoneInfo

        aware = datetime(2026, 9, 20, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles"))
        with patch("app.calendar_client.detect_local_timezone", return_value="America/Los_Angeles"):
            body = build_event_body("Test", "desc", aware, has_time=True)

        assert body["start"]["dateTime"] == "2026-09-20T23:59:00-07:00"

    def test_all_day_event_uses_exclusive_end_date(self):
        body = build_event_body("Test", "desc", datetime(2026, 9, 11), has_time=False)

        assert body["start"]["date"] == "2026-09-11"
        assert body["end"]["date"] == "2026-09-12"  # exclusive end, one day later
        assert "dateTime" not in body["start"]

    def test_recurrence_rule_adds_rrule_with_prefix(self):
        body = build_event_body(
            "Rent", "desc", datetime(2026, 9, 1), has_time=False,
            recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=1",
        )

        assert body["recurrence"] == ["RRULE:FREQ=MONTHLY;BYMONTHDAY=1"]

    def test_no_recurrence_rule_omits_recurrence_key(self):
        body = build_event_body("Test", "desc", datetime(2026, 9, 11), has_time=False)

        assert "recurrence" not in body


class TestEventOperations:
    def test_create_event_returns_id(self):
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "abc123"}

        event_id = create_event(service, "Test", "desc", datetime(2026, 9, 11), has_time=False)

        assert event_id == "abc123"

    def test_create_event_passes_recurrence_rule_through(self):
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "series-1"}

        create_event(
            service, "Standup", "desc", datetime(2026, 9, 14, 9, 0), has_time=True,
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
        )

        body = service.events().insert.call_args.kwargs["body"]
        assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]

    def test_update_event_calls_patch_with_event_id(self):
        service = MagicMock()

        update_event(service, "existing-id", "Test", "desc", datetime(2026, 9, 11, 16, 45), has_time=True)

        service.events().patch.assert_called_with(
            calendarId="primary",
            eventId="existing-id",
            body=service.events().patch.call_args.kwargs["body"],
        )

    def test_delete_event_calls_delete_with_event_id(self):
        service = MagicMock()

        delete_event(service, "existing-id")

        service.events().delete.assert_called_with(calendarId="primary", eventId="existing-id")

    @pytest.mark.parametrize("status", [404, 410])
    def test_deleting_an_already_gone_event_is_treated_as_success(self, status):
        """A retry, or an event someone already deleted directly in Calendar, must not crash
        Remove and leave the dashboard row stuck forever (a real 2026-09-22 incident: a 410 on
        two already-deleted duplicate events made every Remove click on them fail)."""
        service = MagicMock()
        service.events().delete().execute.side_effect = _http_error(status)

        delete_event(service, "already-gone")  # must not raise

    @pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
    def test_a_real_delete_failure_still_raises(self, status):
        service = MagicMock()
        service.events().delete().execute.side_effect = _http_error(status)

        with pytest.raises(HttpError):
            delete_event(service, "some-id")
