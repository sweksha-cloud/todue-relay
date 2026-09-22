import pytest

from app.gmail_client import _has_calendar_invite, _to_email_message


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
