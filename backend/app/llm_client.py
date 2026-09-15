"""Gemini client for the deadline-extraction step.

See app/prompts.py for the prompt text and app/schemas.py for the
structured-output contract this is constrained to.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache

from google import genai
from google.genai import types

from app.config import GEMINI_API_KEY, GEMINI_MIN_INTERVAL_SECONDS, GEMINI_MODEL
from app.gmail_client import EmailMessage
from app.prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt
from app.schemas import ExtractionResult, RawExtraction, parse_llm_response

_rate_limit_lock = threading.Lock()
_last_call_at: float = 0.0


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
    API failure (network, auth, rate limit), which is left to propagate as-is.
    """
    _wait_for_rate_limit()
    response = _get_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=build_extraction_prompt(email),
        config=types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RawExtraction,
        ),
    )
    return parse_llm_response(response.text)
