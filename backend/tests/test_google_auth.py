"""_save_token must keep the database (source of truth) saved even when the
local mirror file can't be written — the AWS Lambda case, where the
filesystem is read-only except /tmp. No Postgres needed: the repository call
is stubbed, since this is about ordering and failure isolation, not SQL.
"""

from types import SimpleNamespace

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
