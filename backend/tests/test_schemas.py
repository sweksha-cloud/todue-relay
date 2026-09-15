import json

import pytest

from app.date_utils import UnparseableDateError
from app.schemas import ExtractionParseError, parse_llm_response


class TestParseLlmResponse:
    def test_valid_response_with_deadline(self):
        raw = json.dumps({
            "email_id": "abc123",
            "event_name": "Assignment 3",
            "deadline_date": "April 18, 2026 at 11:59pm",
            "source_context": "due 4/18",
            "confidence": "high",
            "action_type": "deadline",
        })

        result = parse_llm_response(raw)

        assert result.email_id == "abc123"
        assert result.deadline_date is not None
        assert result.deadline_date.hour == 23 and result.deadline_date.minute == 59
        assert result.action_type == "deadline"

    def test_no_deadline_found(self):
        raw = json.dumps({
            "email_id": "xyz789",
            "event_name": "Newsletter",
            "deadline_date": None,
            "source_context": "no deadline found",
            "confidence": "low",
            "action_type": "unclear",
        })

        result = parse_llm_response(raw)

        assert result.deadline_date is None
        assert result.action_type == "unclear"

    def test_missing_required_field_raises_parse_error(self):
        raw = json.dumps({"email_id": "bad"})

        with pytest.raises(ExtractionParseError):
            parse_llm_response(raw)

    def test_invalid_confidence_value_raises_parse_error(self):
        raw = json.dumps({
            "email_id": "x", "event_name": "x", "deadline_date": None,
            "source_context": "x", "confidence": "medium", "action_type": "unclear",
        })

        with pytest.raises(ExtractionParseError):
            parse_llm_response(raw)

    def test_unparseable_date_string_raises_distinctly(self):
        """Callers need to tell 'garbage JSON' apart from 'valid JSON with
        an unparseable date string' — these propagate as different
        exception types on purpose.
        """
        raw = json.dumps({
            "email_id": "z1", "event_name": "x", "deadline_date": "sometime whenever",
            "source_context": "x", "confidence": "low", "action_type": "deadline",
        })

        with pytest.raises(UnparseableDateError):
            parse_llm_response(raw)

    def test_malformed_json_raises_parse_error(self):
        with pytest.raises(ExtractionParseError):
            parse_llm_response("{not valid json")
