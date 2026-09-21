"""The add-on API's login check: only a genuine Google token, issued for this client, for this one
email, gets in. Real RS256 tokens are signed with a throwaway key and verified against a fake
Google key endpoint, so this exercises the real verification code with no network.
"""

import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt
from google.auth import exceptions as google_exceptions

from app import addon_auth, config
from app.addon_app import app
from app.db.session import get_db

AUDIENCE = "client-123.apps.googleusercontent.com"
OWNER = "owner@example.com"
URL = "/api/addon/summary"


def _keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


GOOGLE_PRIVATE, GOOGLE_PUBLIC = _keypair()  # stands in for Google's signing key
ATTACKER_PRIVATE, _ = _keypair()  # a key Google never published


def _token(*, key=GOOGLE_PRIVATE, **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": OWNER,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(claims)
    signer = crypt.RSASigner.from_string(key, key_id="k1")
    return jwt.encode(signer, payload).decode()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeResponse:
    status = 200

    def __init__(self, certs):
        self.data = json.dumps(certs).encode()


class _FakeGoogleKeys:
    """Stands in for the HTTP call to Google's public signing keys."""

    def __call__(self, url, method="GET", **kwargs):
        return _FakeResponse({"k1": GOOGLE_PUBLIC.decode()})


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setattr(config, "ADDON_OAUTH_CLIENT_ID", AUDIENCE)
    monkeypatch.setattr(config, "ADDON_ALLOWED_EMAIL", OWNER)
    monkeypatch.setattr(addon_auth, "_get_request", lambda: _FakeGoogleKeys())
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestAcceptsOnlyTheOwner:
    def test_a_genuine_token_for_the_owner_is_let_in(self, client):
        assert client.get(URL, headers=_auth(_token())).status_code == 200

    def test_the_email_comparison_ignores_case(self, client):
        assert client.get(URL, headers=_auth(_token(email="Owner@Example.COM"))).status_code == 200

    def test_a_different_google_account_is_refused(self, client):
        response = client.get(URL, headers=_auth(_token(email="someone-else@example.com")))

        assert response.status_code == 403

    def test_an_unverified_email_is_refused_even_if_it_looks_right(self, client):
        response = client.get(URL, headers=_auth(_token(email_verified=False)))

        assert response.status_code == 401

    def test_a_token_with_no_email_claim_is_refused(self, client):
        token = _token()
        claims = jwt.decode(token, certs={"k1": GOOGLE_PUBLIC.decode()}, audience=AUDIENCE)
        claims.pop("email")
        forged = jwt.encode(crypt.RSASigner.from_string(GOOGLE_PRIVATE, key_id="k1"), claims).decode()

        assert client.get(URL, headers=_auth(forged)).status_code in (401, 403)


class TestRefusesBadTokens:
    def test_no_header_at_all(self, client):
        assert client.get(URL).status_code == 401

    def test_a_header_that_is_not_a_bearer_token(self, client):
        assert client.get(URL, headers={"Authorization": "Basic abc"}).status_code == 401

    def test_an_empty_bearer_token(self, client):
        assert client.get(URL, headers={"Authorization": "Bearer "}).status_code == 401

    def test_garbage_instead_of_a_token(self, client):
        assert client.get(URL, headers=_auth("not.a.jwt")).status_code == 401

    def test_an_expired_token(self, client):
        response = client.get(URL, headers=_auth(_token(exp=int(time.time()) - 60)))

        assert response.status_code == 401

    def test_a_token_issued_for_a_different_app(self, client):
        response = client.get(URL, headers=_auth(_token(aud="someone-elses-app.apps.googleusercontent.com")))

        assert response.status_code == 401

    def test_a_token_from_a_different_issuer(self, client):
        response = client.get(URL, headers=_auth(_token(iss="https://evil.example.com")))

        assert response.status_code == 401

    def test_a_token_signed_with_a_key_google_never_published(self, client):
        forged = _token(key=ATTACKER_PRIVATE)

        assert client.get(URL, headers=_auth(forged)).status_code == 401

    def test_a_tampered_token(self, client):
        header, payload, signature = _token().split(".")
        tampered = f"{header}.{payload[:-2]}AA.{signature}"

        assert client.get(URL, headers=_auth(tampered)).status_code == 401

    def test_the_rejection_does_not_say_why(self, client):
        detail = client.get(URL, headers=_auth(_token(aud="other"))).json()["detail"]

        assert detail == "Not authenticated."


class TestFailsClosed:
    def test_with_no_client_id_configured_even_a_valid_token_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(config, "ADDON_OAUTH_CLIENT_ID", "")

        assert client.get(URL, headers=_auth(_token())).status_code == 503

    def test_with_no_allowed_email_configured_even_a_valid_token_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(config, "ADDON_ALLOWED_EMAIL", "")

        assert client.get(URL, headers=_auth(_token())).status_code == 503

    def test_if_googles_keys_cannot_be_fetched_nothing_is_let_in(self, client, monkeypatch):
        def unreachable(url, method="GET", **kwargs):
            raise google_exceptions.TransportError("no network")

        monkeypatch.setattr(addon_auth, "_get_request", lambda: unreachable)

        assert client.get(URL, headers=_auth(_token())).status_code == 503


class TestNothingElseIsExposed:
    """The add-on app is what gets a public address; the unauthenticated dashboard must not ride along."""

    @pytest.mark.parametrize("path", ["/", "/metrics", "/docs", "/redoc", "/openapi.json"])
    def test_dashboard_and_docs_routes_do_not_exist_on_this_app(self, client, path):
        assert client.get(path).status_code == 404


class TestEveryRouteIsProtected:
    """Not only the summary: every action that changes the calendar needs the owner's token."""

    WRITES = [("vote", {"json": {"vote": "correct"}}), ("approve", {}), ("decline", {}), ("remove", {})]

    @pytest.mark.parametrize("action,kwargs", WRITES)
    def test_no_token_means_401(self, client, action, kwargs):
        assert client.post(f"/api/addon/emails/e1/{action}", **kwargs).status_code == 401

    @pytest.mark.parametrize("action,kwargs", WRITES)
    def test_someone_elses_valid_google_token_means_403(self, client, action, kwargs):
        headers = _auth(_token(email="someone-else@example.com"))

        assert client.post(f"/api/addon/emails/e1/{action}", headers=headers, **kwargs).status_code == 403

    def test_a_refused_caller_cannot_delete_a_calendar_event(self, client, db_session, monkeypatch):
        from app import calendar_client
        from app.db import repository
        from app.schemas import ExtractionResult

        deleted = []
        monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
        monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: deleted.append(event_id))
        repository.try_claim_email(db_session, "e1", "t", "Rent due")
        repository.mark_completed(
            db_session, "e1",
            ExtractionResult(email_id="e1", event_name="Rent due", deadline_date_raw="in 3 days", deadline_date=None,
                             source_context="c", confidence="high", action_type="deadline"),
            calendar_event_id="cal-1",
        )

        response = client.post("/api/addon/emails/e1/remove", headers=_auth(_token(email="attacker@example.com")))

        assert response.status_code == 403
        assert deleted == []
