"""Shared OAuth for Gmail (read) + Calendar (events write) — one consent
flow, one cached token, instead of a separate one per API.
"""

from __future__ import annotations

import json
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import GMAIL_CLIENT_SECRET_PATH, GMAIL_TOKEN_PATH, GOOGLE_SCOPES
from app.db import repository
from app.db.session import engine, get_session

logger = logging.getLogger(__name__)

OAUTH_TOKEN_KEY = "google"


def _load_cached_token_json() -> str | None:
    """Database first — the durable source of truth, reachable regardless
    of which compute is running (GitHub Actions today, Lambda later).
    GMAIL_TOKEN_PATH is only a local-dev fallback/mirror: it's what a fresh
    checkout with no DB configured falls back to, but it doesn't exist at
    all on an ephemeral runner.
    """
    if engine is not None:
        session = get_session()
        try:
            token_json = repository.get_oauth_token(session, OAUTH_TOKEN_KEY)
            if token_json:
                return token_json
        finally:
            session.close()
    if GMAIL_TOKEN_PATH.exists():
        return GMAIL_TOKEN_PATH.read_text()
    return None


def _save_token(creds: Credentials) -> None:
    token_json = creds.to_json()
    # Database first: it's the source of truth, so a failure writing the
    # local mirror below can never cost us the refreshed token.
    if engine is not None:
        session = get_session()
        try:
            repository.save_oauth_token(session, OAUTH_TOKEN_KEY, token_json)
        finally:
            session.close()
    # Local-dev convenience/mirror only, best effort: on a read-only
    # filesystem (AWS Lambda, where only /tmp is writable) this raises, and
    # that must not fail an otherwise-successful token refresh.
    try:
        GMAIL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GMAIL_TOKEN_PATH.write_text(token_json)
    except OSError:
        logger.warning("Could not write the local token mirror at %s; the database copy is saved", GMAIL_TOKEN_PATH)


def get_google_credentials() -> Credentials:
    """Return valid credentials covering GOOGLE_SCOPES, refreshing or
    running the interactive consent flow as needed, and persisting the
    result to the database (so it survives on any compute target) and a
    local file (dev convenience/mirror only).
    """
    creds = None
    cached = _load_cached_token_json()
    if cached:
        creds = Credentials.from_authorized_user_info(json.loads(cached), GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CLIENT_SECRET_PATH.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secret at {GMAIL_CLIENT_SECRET_PATH}. "
                    "Download it from Google Cloud Console (OAuth client, Desktop app "
                    "type) and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CLIENT_SECRET_PATH), GOOGLE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        _save_token(creds)

    return creds
