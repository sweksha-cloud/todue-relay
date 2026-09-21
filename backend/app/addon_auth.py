"""Who may call the Gmail add-on API: one Google account, proven by a Google identity token.

The add-on runs on Google's servers (Apps Script) and cannot hold a secret safely, so it sends the
short-lived OpenID Connect token Google issues for the signed-in user (ScriptApp.getIdentityToken).
This module checks, in order:

  1. the API is configured at all (an audience and an allowed email), else it refuses everything;
  2. the token's signature is valid against Google's published keys, it has not expired, the issuer
     is Google, and it was issued for OUR client id (a token minted for some other app is rejected);
  3. the email is verified by Google and equals the one allowed address.

Failures are deliberately terse: a caller learns "not authenticated" or "not you", never why a
token was rejected.
"""

from __future__ import annotations

import time

from fastapi import Header, HTTPException
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app import config

_CERTS_TTL_SECONDS = 3600


class _CachedCertsRequest:
    """Wraps google-auth's HTTP transport so Google's signing keys are fetched once an hour, not on
    every request. Only GETs are cached; the keys rotate slowly and are public."""

    def __init__(self) -> None:
        self._inner = google_requests.Request()
        self._cache: dict[str, tuple[float, object]] = {}

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        now = time.monotonic()
        hit = self._cache.get(url)
        if method == "GET" and hit and now - hit[0] < _CERTS_TTL_SECONDS:
            return hit[1]
        response = self._inner(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        if method == "GET" and response.status == 200:
            self._cache[url] = (now, response)
        return response


_request = _CachedCertsRequest()


def _get_request():
    """The transport used to fetch Google's keys. A function so tests can substitute their own."""
    return _request


def verify_google_id_token(token: str, audience: str) -> dict:
    """The verified claims of a Google-issued identity token, or ValueError / GoogleAuthError."""
    return id_token.verify_oauth2_token(token, _get_request(), audience=audience)


def require_owner(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: the authenticated owner's email, or an HTTP error."""
    if not config.ADDON_OAUTH_CLIENT_ID or not config.ADDON_ALLOWED_EMAIL:
        raise HTTPException(status_code=503, detail="The add-on API is not configured.")

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Not authenticated.")

    try:
        claims = verify_google_id_token(token.strip(), audience=config.ADDON_OAUTH_CLIENT_ID)
    except google_exceptions.TransportError as e:  # could not reach Google for its keys
        raise HTTPException(status_code=503, detail="Could not verify the token right now.") from e
    except (ValueError, google_exceptions.GoogleAuthError) as e:
        raise HTTPException(status_code=401, detail="Not authenticated.") from e

    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    email = str(claims.get("email", "")).strip().lower()
    if email != config.ADDON_ALLOWED_EMAIL:
        raise HTTPException(status_code=403, detail="Not allowed.")
    return email
