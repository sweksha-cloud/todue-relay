"""Gemini client for the deadline-extraction step.

See app/prompts.py for the prompt text and app/schemas.py for the
structured-output contract this is constrained to.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache

import httpx  # a hard dependency of google-genai, so always present
from google import genai
from google.genai import errors, types

from app.config import GEMINI_API_KEY, GEMINI_MIN_INTERVAL_SECONDS, GEMINI_MODEL
from app.gmail_client import EmailMessage
from app.prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt
from app.schemas import ExtractionResult, RawExtraction, parse_llm_response

_rate_limit_lock = threading.Lock()
_last_call_at: float = 0.0


class LLMTransientError(Exception):
    """The call failed for a reason that says nothing about this email: Gemini answered with a 5xx
    ("503 UNAVAILABLE, high demand" is the common one) or the connection dropped. A retry later will
    likely succeed, so the pipeline refunds the attempt instead of counting it toward
    MAX_ATTEMPTS_PER_EMAIL, within a time bound (config.TRANSIENT_RETRY_WINDOW_HOURS). The message keeps
    the API's full error text so it stays visible in the run's error_message.
    """


class LLMRateLimitError(LLMTransientError):
    """The API rejected the call with a 429 — a per-minute or per-day quota
    was hit. Says nothing about the email itself (a retry after the quota
    resets will likely succeed), so it is transient like any other. The message keeps the
    API's full error text, including the quotaId/quotaValue that shows *which* limit was hit.
    """


def _wait_for_rate_limit() -> None:
    """Block until at least GEMINI_MIN_INTERVAL_SECONDS has passed since the
    last call. A real run without this hit the free tier's 5-requests/minute
    limit repeatedly (14 of 19 extraction attempts failed with 429
    RESOURCE_EXHAUSTED) — this is Step 5's "account for rate limits"
    requirement, observed as a real failure, not a hypothetical one.
    """
    global _last_call_at
    with _rate_limit_lock:
        elapsed = time.monotonic() - _last_call_at
        wait = GEMINI_MIN_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


@lru_cache(maxsize=1)
def _get_client() -> genai.Client:
    # Built lazily so importing this module doesn't require GEMINI_API_KEY
    # to already be set (e.g. before it's been added to .env).
    return genai.Client(api_key=GEMINI_API_KEY)


def extract_deadline(email: EmailMessage) -> ExtractionResult:
    """Call Gemini to extract deadline info from one pre-filtered email.

    Raises ExtractionParseError / UnparseableDateError per the contract in
    app/schemas.py — callers handle those distinctly from a transport-level
    API failure: a 429, a 5xx or a dropped connection raises LLMTransientError (see above); anything
    else (auth, a bad request) propagates as-is.
    """
    _wait_for_rate_limit()
    try:
        response = _get_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=build_extraction_prompt(email),
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=RawExtraction,
            ),
        )
    except errors.ClientError as e:
        if e.code == 429:
            raise LLMRateLimitError(str(e)) from e
        raise  # a 400 and the like are about this email's request: they count
    except errors.ServerError as e:
        raise LLMTransientError(str(e)) from e
    except httpx.TransportError as e:  # connection refused or reset, a timeout
        raise LLMTransientError(f"{type(e).__name__}: {e}") from e
    return parse_llm_response(response.text)
