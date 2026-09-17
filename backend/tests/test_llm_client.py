"""The 429 -> LLMRateLimitError translation. No real API calls: the Gemini
client is replaced with a stub that raises whatever error is under test.
"""

import pytest
from google.genai import errors

from app import llm_client
from app.gmail_client import EmailMessage


def _email() -> EmailMessage:
    return EmailMessage(
        id="e1", thread_id="t1", subject="Due Friday", sender="a@b.com",
        date="", snippet="", body_text="Submit by Friday.",
    )


class _RaisingClient:
    def __init__(self, exc):
        self.models = self
        self._exc = exc

    def generate_content(self, **kwargs):
        raise self._exc


@pytest.fixture(autouse=True)
def _no_pacing_sleep(monkeypatch):
    monkeypatch.setattr(llm_client, "_wait_for_rate_limit", lambda: None)


class TestRateLimitTranslation:
    def test_429_becomes_llm_rate_limit_error_keeping_the_quota_details(self, monkeypatch):
        body = {"error": {"code": 429, "message": "quota", "details": [{"quotaId": "PerDay", "quotaValue": "20"}]}}
        monkeypatch.setattr(llm_client, "_get_client", lambda: _RaisingClient(errors.ClientError(429, body)))

        with pytest.raises(llm_client.LLMRateLimitError) as exc_info:
            llm_client.extract_deadline(_email())

        assert "quotaValue" in str(exc_info.value)  # which limit was hit stays visible in error_message

    def test_other_client_errors_pass_through_unchanged(self, monkeypatch):
        """A 400 (bad request) is a real failure that should count toward
        the retry cap — only a 429 gets the special treatment.
        """
        body = {"error": {"code": 400, "message": "bad request"}}
        monkeypatch.setattr(llm_client, "_get_client", lambda: _RaisingClient(errors.ClientError(400, body)))

        with pytest.raises(errors.ClientError):
            llm_client.extract_deadline(_email())
