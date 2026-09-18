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
    def test_flags_calendar_invite_on_the_message(self):
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
        assert email.has_calendar_invite is True

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
        assert email.has_calendar_invite is False
