"""Shared OAuth for Gmail (read) + Calendar (events write) — one consent
flow, one cached token, instead of a separate one per API.
"""

from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import GMAIL_CLIENT_SECRET_PATH, GMAIL_TOKEN_PATH, GOOGLE_SCOPES


def get_google_credentials() -> Credentials:
    """Return valid credentials covering GOOGLE_SCOPES, refreshing or
    running the interactive consent flow as needed, and caching the result.
    """
    creds = None
    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GOOGLE_SCOPES)

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

        GMAIL_TOKEN_PATH.write_text(creds.to_json())

    return creds
