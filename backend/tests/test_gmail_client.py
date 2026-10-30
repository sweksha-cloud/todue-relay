import pytest
from googleapiclient.errors import HttpError

from app.gmail_client import (
    GMAIL_NUM_RETRIES,
    _has_calendar_invite,
    _to_email_message,
    fetch_messages_by_ids,
    fetch_recent_messages,
    trash_message,
)


def _http_error(status: int) -> HttpError:
    from types import SimpleNamespace

    return HttpError(resp=SimpleNamespace(status=status, reason="x"), content=b"{}")


class TestHasCalendarInvite:
    def test_top_level_text_calendar_mimetype(self):
        payload = {"mimeType": "text/calendar", "filename": "invite.ics"}
        assert _has_calendar_invite(payload) is True

    def test_nested_text_calendar_part(self):
        """Real shape confirmed against a live inbox (2026-09-18): a Zoom
        confirmation email — multipart/mixed -> multipart/alternative ->
        [text/html, text/calendar]."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": "aGk="}},
                        {"mimeType": "text/calendar", "filename": "invite.ics", "body": {"data": "..."}},
                    ],
                },
            ],
        }
        assert _has_calendar_invite(payload) is True

    def test_application_ics_mimetype(self):
        """A second real shape seen in the same inbox: some clients send
        the invite as application/ics instead of text/calendar."""
        payload = {"mimeType": "application/ics", "filename": "invite.ics"}
        assert _has_calendar_invite(payload) is True

    def test_ics_filename_without_matching_mimetype(self):
        payload = {"mimeType": "application/octet-stream", "filename": "invite.ics"}
        assert _has_calendar_invite(payload) is True

    def test_plain_email_has_no_invite(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "aGk="}},
                {"mimeType": "text/html", "body": {"data": "aGk="}},
            ],
        }
        assert _has_calendar_invite(payload) is False

    def test_empty_payload_has_no_invite(self):
        assert _has_calendar_invite({}) is False


class TestToEmailMessage:
    def test_flags_a_real_ics_invite_on_the_message(self):
        msg = {
            "id": "e1",
            "threadId": "t1",
            "snippet": "snippet",
            "payload": {
                "headers": [{"name": "Subject", "value": "Invitation: Career closet"}],
                "mimeType": "multipart/mixed",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "aGk="}},
                    {"mimeType": "text/calendar", "filename": "invite.ics", "body": {"data": "..."}},
                ],
            },
        }
        email = _to_email_message(msg)
        assert email.already_on_calendar is True

    @pytest.mark.parametrize(
        "subject",
        [
            "New event: CS 157C Zoom Lecture",
            "Canceled event: Career closet",
            "Updated invitation: MATH 167R FINAL EXAM",
            "Accepted: Dentist Appointment",
        ],
    )
    def test_flags_any_google_calendar_notification_even_with_no_ics_attachment(self, subject):
        """Real shapes confirmed against a live inbox (2026-09-21): none of these four has an .ics
        attachment or a distinguishing subject word, but all four carry this exact Sender — the
        .ics-only check let every one of them through (docs/design-decisions.md, decision 6 amendment).
        An earlier version of the fix checked the X-Google-Calendar-Notification header instead;
        it was present on only two of these four (missed "Updated invitation:" and "Accepted:"),
        which is why the check is on Sender instead.
        """
        msg = {
            "id": "e1",
            "threadId": "t1",
            "snippet": "snippet",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"},
                ],
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "aGk="}},
                    {"mimeType": "text/html", "body": {"data": "aGk="}},
                ],
            },
        }
        email = _to_email_message(msg)
        assert email.already_on_calendar is True

    def test_flags_a_notification_whose_sender_is_in_the_from_header_instead(self):
        """Real shape (2026-09-14, 'New event: SpartUp Founder Fiesta'): no Sender header at
        all — the calendar-notification address is in From instead: '"SpartUp (Google Calendar)"
        <calendar-notification@google.com>'. Checking only Sender missed this one."""
        msg = {
            "id": "e1", "threadId": "t1", "snippet": "s",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "New event: SpartUp Founder Fiesta"},
                    {"name": "From", "value": '"SpartUp (Google Calendar)" <calendar-notification@google.com>'},
                ],
                "mimeType": "text/plain", "body": {"data": "aGk="},
            },
        }
        assert _to_email_message(msg).already_on_calendar is True

    def test_the_sender_match_is_case_insensitive(self):
        msg = {
            "id": "e1", "threadId": "t1", "snippet": "s",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "hi"},
                    {"name": "Sender", "value": "Google Calendar <Calendar-Notification@Google.com>"},
                ],
                "mimeType": "text/plain", "body": {"data": "aGk="},
            },
        }
        assert _to_email_message(msg).already_on_calendar is True

    def test_a_real_invite_also_carries_the_sender_but_the_ics_check_alone_already_caught_it(self):
        """The 'Invitation: ...' case: this one has both a real .ics attachment AND the Sender —
        either check alone would flag it; this pins that both paths agree."""
        msg = {
            "id": "e1", "threadId": "t1", "snippet": "s",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Invitation: Career closet"},
                    {"name": "Sender", "value": "Google Calendar <calendar-notification@google.com>"},
                ],
                "mimeType": "multipart/mixed",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "aGk="}},
                    {"mimeType": "text/calendar", "filename": "invite.ics", "body": {"data": "..."}},
                ],
            },
        }
        assert _to_email_message(msg).already_on_calendar is True

    def test_plain_email_is_not_flagged(self):
        msg = {
            "id": "e1",
            "threadId": "t1",
            "snippet": "snippet",
            "payload": {
                "headers": [{"name": "Subject", "value": "Assignment 3 due Friday"}],
                "mimeType": "text/plain",
                "body": {"data": "aGk="},
            },
        }
        email = _to_email_message(msg)
        assert email.already_on_calendar is False

    def test_a_header_present_but_empty_does_not_flag_it(self):
        """The header's presence is the signal; an empty value (unlikely, but seen inconsistently
        across mail headers generally) should not count."""
        msg = {
            "id": "e1", "threadId": "t1", "snippet": "s",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "hi"},
                    {"name": "X-Google-Calendar-Notification", "value": ""},
                ],
                "mimeType": "text/plain", "body": {"data": "aGk="},
            },
        }
        assert _to_email_message(msg).already_on_calendar is False


class TestTrashMessage:
    def test_trashes_the_given_message(self):
        from unittest.mock import MagicMock

        service = MagicMock()

        trash_message(service, "e1")

        service.users().messages().trash.assert_called_with(userId="me", id="e1")

    def test_a_message_already_gone_is_treated_as_success(self):
        from unittest.mock import MagicMock

        service = MagicMock()
        service.users().messages().trash().execute.side_effect = _http_error(404)

        trash_message(service, "already-gone")  # must not raise

    @pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
    def test_a_real_failure_still_raises(self, status):
        from unittest.mock import MagicMock

        service = MagicMock()
        service.users().messages().trash().execute.side_effect = _http_error(status)

        with pytest.raises(HttpError):
            trash_message(service, "e1")


def _gmail_message(message_id: str) -> dict:
    return {
        "id": message_id, "threadId": "t", "snippet": "",
        "payload": {"headers": [{"name": "Subject", "value": f"subject {message_id}"}], "mimeType": "text/plain", "body": {}},
    }


class TestFetchingSurvivesGmailRateLimits:
    """A catch-up run reads hundreds of messages, and Gmail answers a burst with 403
    "rateLimitExceeded". Every read retries with backoff; one that still fails is skipped with a
    single summary line, not one error per email."""

    def _service(self, failing_ids=(), status=403):
        from unittest.mock import MagicMock

        service = MagicMock()
        service.users().messages().list().execute.return_value = {"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}

        def get(userId, id, format):
            request = MagicMock()
            if id in failing_ids:
                request.execute.side_effect = _http_error(status)
            else:
                request.execute.return_value = _gmail_message(id)
            return request

        service.users().messages().get.side_effect = get
        return service

    def test_every_read_asks_googleapiclient_to_retry_with_backoff(self):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.execute.return_value = _gmail_message("a")
        service = MagicMock()
        service.users().messages().get.return_value = request

        fetch_messages_by_ids(service, ["a"])

        request.execute.assert_called_with(num_retries=GMAIL_NUM_RETRIES)

    def test_a_message_still_rate_limited_is_skipped_and_the_rest_are_returned(self):
        messages = fetch_recent_messages(self._service(failing_ids={"b"}), query="is:unread")

        assert [m.id for m in messages] == ["a", "c"]

    def test_skips_are_one_summary_line_not_one_per_email(self, caplog, capsys):
        with caplog.at_level("WARNING", logger="app.gmail_client"):
            fetch_recent_messages(self._service(failing_ids={"a", "b"}), query="is:unread")

        assert capsys.readouterr().out == ""  # nothing printed per message
        warnings = [r.getMessage() for r in caplog.records if r.name == "app.gmail_client"]
        assert warnings == ["Skipped 2 of 3 emails Gmail didn't return (2 rate-limited by Gmail); a later run picks them up"]
