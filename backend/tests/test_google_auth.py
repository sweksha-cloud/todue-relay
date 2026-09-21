"""_save_token must keep the database (source of truth) saved even when the
local mirror file can't be written — the AWS Lambda case, where the
filesystem is read-only except /tmp. No Postgres needed: the repository call
is stubbed, since this is about ordering and failure isolation, not SQL.
"""

from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

from app import google_auth


class _FakeCreds:
    def to_json(self) -> str:
        return '{"token": "t"}'


class _FakeSession:
    def close(self) -> None:
        pass


def _stub_db(monkeypatch, saved: list):
    monkeypatch.setattr(google_auth, "engine", object())
    monkeypatch.setattr(google_auth, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(
        google_auth.repository,
        "save_oauth_token",
        lambda session, key, token_json: saved.append((key, token_json)),
    )


def test_save_token_writes_db_and_local_mirror(monkeypatch, tmp_path):
    mirror = tmp_path / "creds" / "gmail_token.json"  # parent dir doesn't exist yet
    monkeypatch.setattr(google_auth, "GMAIL_TOKEN_PATH", mirror)
    saved: list = []
    _stub_db(monkeypatch, saved)

    google_auth._save_token(_FakeCreds())

    assert saved == [("google", '{"token": "t"}')]
    assert mirror.read_text() == '{"token": "t"}'


def test_save_token_survives_unwritable_mirror(monkeypatch):
    class _ReadOnlyPath:
        parent = SimpleNamespace(mkdir=lambda **kw: (_ for _ in ()).throw(OSError(30, "Read-only file system")))

        def write_text(self, _):
            raise OSError(30, "Read-only file system")

    monkeypatch.setattr(google_auth, "GMAIL_TOKEN_PATH", _ReadOnlyPath())
    saved: list = []
    _stub_db(monkeypatch, saved)

    google_auth._save_token(_FakeCreds())  # must not raise

    assert saved == [("google", '{"token": "t"}')]  # DB copy still saved


def test_save_token_db_saved_before_mirror_is_attempted(monkeypatch):
    order: list[str] = []

    class _Path:
        parent = SimpleNamespace(mkdir=lambda **kw: None)

        def write_text(self, _):
            order.append("mirror")

    monkeypatch.setattr(google_auth, "GMAIL_TOKEN_PATH", _Path())
    monkeypatch.setattr(google_auth, "engine", object())
    monkeypatch.setattr(google_auth, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(
        google_auth.repository, "save_oauth_token", lambda *a: order.append("db")
    )

    google_auth._save_token(_FakeCreds())

    assert order == ["db", "mirror"]


class _FakeStoredCreds:
    """A stored token that has expired but still has a refresh token."""

    valid = False
    expired = True
    refresh_token = "r"

    def __init__(self, refresh_error=None):
        self.refresh_error = refresh_error
        self.refreshed = False

    def refresh(self, request):
        if self.refresh_error:
            raise self.refresh_error
        self.refreshed = True


def _stub_stored_token(monkeypatch, creds):
    monkeypatch.setattr(google_auth, "_load_cached_token_json", lambda: "{}")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_info", lambda info, scopes: creds)


class TestExpiredRefreshToken:
    def test_a_rejected_refresh_token_raises_an_error_that_says_how_to_fix_it(self, monkeypatch):
        _stub_stored_token(monkeypatch, _FakeStoredCreds(RefreshError("invalid_grant: Token has been expired or revoked.")))

        with pytest.raises(google_auth.GoogleReauthRequired) as exc:
            google_auth.get_google_credentials()

        assert "python -m scripts.reauth_google" in str(exc.value)
        assert "invalid_grant" in str(exc.value)  # Google's own reason is kept

    def test_a_working_refresh_token_is_refreshed_and_saved(self, monkeypatch):
        creds = _FakeStoredCreds()
        _stub_stored_token(monkeypatch, creds)
        saved = []
        monkeypatch.setattr(google_auth, "_save_token", lambda c: saved.append(c))

        assert google_auth.get_google_credentials() is creds
        assert creds.refreshed and saved == [creds]


class TestReauthorize:
    def test_runs_the_consent_flow_and_saves_the_new_token(self, monkeypatch, tmp_path):
        secret = tmp_path / "client_secret.json"
        secret.write_text("{}")
        monkeypatch.setattr(google_auth, "GMAIL_CLIENT_SECRET_PATH", secret)
        new_creds = object()
        flow = SimpleNamespace(run_local_server=lambda port: new_creds)
        monkeypatch.setattr(google_auth.InstalledAppFlow, "from_client_secrets_file", lambda path, scopes: flow)
        saved = []
        monkeypatch.setattr(google_auth, "_save_token", lambda c: saved.append(c))

        assert google_auth.reauthorize_google() is new_creds
        assert saved == [new_creds]

    def test_no_stored_token_starts_the_consent_flow(self, monkeypatch):
        monkeypatch.setattr(google_auth, "_load_cached_token_json", lambda: None)
        started = []
        monkeypatch.setattr(google_auth, "reauthorize_google", lambda: started.append(True) or "fresh")

        assert google_auth.get_google_credentials() == "fresh"
        assert started == [True]

    def test_a_missing_client_secret_says_where_to_get_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(google_auth, "GMAIL_CLIENT_SECRET_PATH", tmp_path / "nope.json")

        with pytest.raises(FileNotFoundError, match="client secret"):
            google_auth.reauthorize_google()
