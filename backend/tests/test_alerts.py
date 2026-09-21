"""Alerting: an email through the SNS topic when something needs a person. No real AWS: the SNS client is a fake."""

import pytest

from app import alerts, config

TOPIC = "arn:aws:sns:us-east-2:123456789012:todue-alerts"


class _FakeSns:
    def __init__(self, error=None):
        self.published = []
        self._error = error

    def publish(self, **kwargs):
        if self._error:
            raise self._error
        self.published.append(kwargs)
        return {"MessageId": "m-1"}


@pytest.fixture(autouse=True)
def topic(monkeypatch):
    monkeypatch.setattr(config, "ALERT_TOPIC_ARN", TOPIC)


class TestSendAlert:
    def test_publishes_to_the_configured_topic(self):
        sns = _FakeSns()

        assert alerts.send_alert("Subject", "Body", client=sns) is True
        assert sns.published == [{"TopicArn": TOPIC, "Subject": "Subject", "Message": "Body"}]

    def test_with_no_topic_configured_it_only_logs(self, monkeypatch):
        monkeypatch.setattr(config, "ALERT_TOPIC_ARN", "")
        sns = _FakeSns()

        assert alerts.send_alert("Subject", "Body", client=sns) is False
        assert sns.published == []

    def test_a_failure_to_send_is_swallowed_and_reported_as_not_sent(self):
        assert alerts.send_alert("Subject", "Body", client=_FakeSns(error=RuntimeError("AccessDenied"))) is False

    def test_the_subject_is_made_safe_for_sns(self):
        sns = _FakeSns()

        alerts.send_alert("Résumé\nreview " + "x" * 200, "Body", client=sns)

        subject = sns.published[0]["Subject"]
        assert subject.isascii() and "\n" not in subject and len(subject) <= 100
        assert subject.startswith("Rsum review")


class TestNotifyParked:
    def _item(self, n, error="ServerError: 503 UNAVAILABLE"):
        return {"id": f"e{n}", "subject": f"Subject {n}", "error": error}

    def test_says_which_emails_and_why(self):
        sns = _FakeSns()

        assert alerts.notify_parked([self._item(1)], max_attempts=3, client=sns) is True

        sent = sns.published[0]
        assert sent["Subject"] == "ToDue Relay: 1 email failed and will not be retried"
        assert "Subject 1" in sent["Message"] and "503 UNAVAILABLE" in sent["Message"]
        assert "used all 3 attempts" in sent["Message"]

    def test_tells_the_reader_what_to_do_about_a_temporary_error(self):
        sns = _FakeSns()

        alerts.notify_parked([self._item(1)], client=sns)

        assert "python -m scripts.retry_failed --apply" in sns.published[0]["Message"]

    def test_several_emails_in_one_run_make_one_alert(self):
        sns = _FakeSns()

        alerts.notify_parked([self._item(1), self._item(2), self._item(3)], client=sns)

        assert len(sns.published) == 1
        assert sns.published[0]["Subject"] == "ToDue Relay: 3 emails failed and will not be retried"

    def test_a_flood_is_counted_not_listed(self):
        sns = _FakeSns()

        alerts.notify_parked([self._item(i) for i in range(25)], client=sns)

        message = sns.published[0]["Message"]
        assert "Subject 9" in message and "Subject 10" not in message
        assert "and 15 more" in message

    def test_nothing_parked_sends_nothing(self):
        sns = _FakeSns()

        assert alerts.notify_parked([], client=sns) is False
        assert sns.published == []

    def test_a_missing_subject_or_error_does_not_break_the_message(self):
        sns = _FakeSns()

        assert alerts.notify_parked([{"id": "e1", "subject": "", "error": None}], client=sns) is True
        assert "(no subject)" in sns.published[0]["Message"]

    def test_a_very_long_error_is_shortened(self):
        sns = _FakeSns()

        alerts.notify_parked([self._item(1, error="E" * 5000)], client=sns)

        assert len(sns.published[0]["Message"]) < 1000
