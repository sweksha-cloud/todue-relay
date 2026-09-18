"""The Lambda wrapper's own logic: secret loading and dry-run event handling.
run_pipeline is stubbed and the Secrets Manager client is a fake, so nothing
here touches AWS, Gemini, Gmail or a database.
"""

import json

import pytest

import lambda_handler


class _FakeSecretsClient:
    def __init__(self, secret: dict | str):
        self.secret = secret
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):
        self.requested.append(SecretId)
        body = self.secret if isinstance(self.secret, str) else json.dumps(self.secret)
        return {"SecretString": body}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("SECRET_ID", *lambda_handler.SECRET_KEYS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lambda_handler, "_initialized", False)


def test_load_secrets_is_noop_without_secret_id(monkeypatch):
    client = _FakeSecretsClient({})
    lambda_handler.load_secrets(client)
    assert client.requested == []


def test_load_secrets_copies_keys_into_environment(monkeypatch):
    import os

    monkeypatch.setenv("SECRET_ID", "todue-relay")
    client = _FakeSecretsClient(
        {"GEMINI_API_KEY": "g-key", "DATABASE_URL": "postgresql://x", "IGNORED": "nope"}
    )

    lambda_handler.load_secrets(client)

    assert client.requested == ["todue-relay"]
    assert os.environ["GEMINI_API_KEY"] == "g-key"
    assert os.environ["DATABASE_URL"] == "postgresql://x"
    assert "IGNORED" not in os.environ  # only the two known keys are copied


def test_load_secrets_names_missing_keys_without_leaking_values(monkeypatch):
    monkeypatch.setenv("SECRET_ID", "todue-relay")
    client = _FakeSecretsClient({"GEMINI_API_KEY": "super-secret-value"})

    with pytest.raises(KeyError) as exc:
        lambda_handler.load_secrets(client)

    assert "DATABASE_URL" in str(exc.value)
    assert "super-secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    "event, expected",
    [
        ({"dry_run": True}, True),
        ({"dry_run": False}, False),
        ({"dry_run": "false"}, False),  # a string must never switch dry-run on
        ({"dry_run": "true"}, False),
        ({"dry_run": 1}, False),
        ({}, False),
        (None, False),
        ({"source": "aws.events", "detail-type": "Scheduled Event"}, False),  # EventBridge's own payload
    ],
)
def test_handler_dry_run_only_for_literal_true(monkeypatch, event, expected):
    calls = []
    monkeypatch.setattr(
        "app.pipeline.run_pipeline", lambda **kw: calls.append(kw) or {"fetched": 0, **kw}
    )

    result = lambda_handler.handler(event, None)

    assert calls == [{"dry_run": expected}]
    assert result["dry_run"] is expected


def test_handler_initializes_once_across_invocations(monkeypatch):
    loads = []
    monkeypatch.setattr(lambda_handler, "load_secrets", lambda: loads.append(1))
    monkeypatch.setattr("app.pipeline.run_pipeline", lambda **kw: {})

    lambda_handler.handler({}, None)
    lambda_handler.handler({}, None)

    assert loads == [1]  # a warm container doesn't refetch the secret


def test_handler_propagates_pipeline_failure(monkeypatch):
    def boom(**kw):
        raise RuntimeError("gmail down")

    monkeypatch.setattr("app.pipeline.run_pipeline", boom)

    with pytest.raises(RuntimeError, match="gmail down"):
        lambda_handler.handler({}, None)  # must raise so Lambda records an error
