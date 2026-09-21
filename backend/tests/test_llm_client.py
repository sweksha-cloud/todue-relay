"""The 429 -> LLMRateLimitError translation. No real API calls: the Gemini
client is replaced with a stub that raises whatever error is under test.
"""

import httpx
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


class TestTransientErrorsAreNotTheEmailsFault:
    """A Gemini 5xx or a dropped connection says nothing about the email, so it becomes a transient error
    the pipeline refunds. A 429 is the same kind of thing, and a bad request is not."""

    def _raise(self, monkeypatch, exc):
        monkeypatch.setattr(llm_client, "_get_client", lambda: _RaisingClient(exc))
        with pytest.raises(Exception) as caught:
            llm_client.extract_deadline(_email())
        return caught.value

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_a_5xx_becomes_a_transient_error_keeping_the_apis_text(self, monkeypatch, code):
        body = {"error": {"code": code, "message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}}

        error = self._raise(monkeypatch, errors.ServerError(code, body))

        assert isinstance(error, llm_client.LLMTransientError)
        assert not isinstance(error, llm_client.LLMRateLimitError)  # a different cause, recorded as such
        assert "high demand" in str(error)

    @pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")])
    def test_a_dropped_or_timed_out_connection_is_transient(self, monkeypatch, exc):
        error = self._raise(monkeypatch, exc)

        assert isinstance(error, llm_client.LLMTransientError)
        assert type(exc).__name__ in str(error)

    def test_a_rate_limit_is_also_transient(self, monkeypatch):
        error = self._raise(monkeypatch, errors.ClientError(429, {"error": {"code": 429, "message": "quota"}}))

        assert isinstance(error, llm_client.LLMRateLimitError)
        assert isinstance(error, llm_client.LLMTransientError)

    @pytest.mark.parametrize(
        "exc",
        [errors.ClientError(400, {"error": {"code": 400, "message": "bad"}}), errors.ClientError(403, {"error": {"code": 403, "message": "no"}}), ValueError("boom")],
    )
    def test_anything_about_the_request_itself_is_not_transient_and_still_counts(self, monkeypatch, exc):
        error = self._raise(monkeypatch, exc)

        assert not isinstance(error, llm_client.LLMTransientError)
