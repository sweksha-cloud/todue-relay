"""Schema for the LLM's structured extraction output.

The extraction step uses Gemini (app/llm_client.py), constrained to this
JSON shape via its structured-output mechanism; validation here is kept
independent of that client so the raw JSON is checked the same way
regardless of who produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.date_utils import UnparseableDateError, parse_deadline_date

ActionType = Literal["deadline", "needs_reply", "unclear"]

EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "email_id": {"type": "string"},
        "event_name": {"type": "string"},
        "deadline_date": {"type": ["string", "null"]},
        "source_context": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "action_type": {"type": "string", "enum": ["deadline", "needs_reply", "unclear"]},
    },
    "required": [
        "email_id",
        "event_name",
        "deadline_date",
        "source_context",
        "confidence",
        "action_type",
    ],
}


class RawExtraction(BaseModel):
    """Exact shape the LLM is asked to return."""

    email_id: str
    event_name: str
    deadline_date: str | None
    source_context: str
    confidence: Literal["high", "low"]
    action_type: ActionType


class ExtractionResult(BaseModel):
    """RawExtraction plus the date resolved to an actual datetime."""

    email_id: str
    event_name: str
    deadline_date_raw: str | None
    deadline_date: datetime | None
    source_context: str
    confidence: Literal["high", "low"]
    action_type: ActionType


class ExtractionParseError(Exception):
    """Raised when the LLM's raw response can't be validated/parsed at all."""


def parse_llm_response(raw_json_text: str) -> ExtractionResult:
    """Validate an LLM response string against the schema and resolve its date.

    Raises ExtractionParseError for malformed JSON / schema violations, and
    lets UnparseableDateError propagate separately for a syntactically valid
    response whose deadline_date string isn't a resolvable date — callers
    handle that case distinctly (e.g. downgrade confidence) rather than
    treating it as the same failure as garbage JSON.
    """
    try:
        raw = RawExtraction.model_validate_json(raw_json_text)
    except ValidationError as e:
        raise ExtractionParseError(f"LLM response failed schema validation: {e}") from e

    parsed_date = parse_deadline_date(raw.deadline_date)  # may raise UnparseableDateError

    return ExtractionResult(
        email_id=raw.email_id,
        event_name=raw.event_name,
        deadline_date_raw=raw.deadline_date,
        deadline_date=parsed_date,
        source_context=raw.source_context,
        confidence=raw.confidence,
        action_type=raw.action_type,
    )
