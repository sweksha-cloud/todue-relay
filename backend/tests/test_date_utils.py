"""Covers real bugs found and fixed during manual testing tonight — these
are the checks that would have caught them automatically instead of
needing a live triggered run to surface them.
"""

from datetime import datetime, timezone

import pytest

from app.date_utils import (
    has_explicit_time,
    is_plausible,
    parse_deadline_date,
)


class TestParseDeadlineDate:
    def test_no_zone_in_string_gets_local_tzinfo(self):
        """Root-cause fix: a naive result used to get silently mislabeled
        as UTC downstream (by both Postgres and the Calendar API), causing
        a real 7-hour-off Calendar event. Now always tz-aware.
        """
        dt = parse_deadline_date("September 11, 2026 at 4:45 PM")
        assert dt.tzinfo is not None
        assert dt.hour == 16 and dt.minute == 45

    def test_known_us_abbreviation_resolved_correctly(self):
        """Confirmed bug: 'EST' used to be silently treated as the local
        machine's zone instead of actual Eastern time — a real 3-hour
        error with no warning.
        """
        dt = parse_deadline_date("September 20, 2026 at 5:00 PM EST")
        assert dt.utcoffset().total_seconds() == -5 * 3600

    def test_unrecognized_abbreviation_falls_back_without_crashing(self):
        """Best-effort fallback for a genuinely unresolvable zone — should
        not raise, even though it can't be fully correct.
        """
        dt = parse_deadline_date("September 20, 2026 at 5:00 PM XYZ")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_input_returns_none(self):
        assert parse_deadline_date(None) is None

    def test_empty_string_returns_none(self):
        assert parse_deadline_date("   ") is None

    def test_unparseable_string_raises(self):
        from app.date_utils import UnparseableDateError

        with pytest.raises(UnparseableDateError):
            parse_deadline_date("this is not a date at all zzz")


class TestHasExplicitTime:
    def test_detects_explicit_time(self):
        assert has_explicit_time("September 11, 2026 at 4:45 PM") is True

    def test_date_only_has_no_explicit_time(self):
        assert has_explicit_time("September 11, 2026") is False

    def test_none_input(self):
        assert has_explicit_time(None) is False


class TestIsPlausible:
    def test_within_bounds_is_plausible(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        dt = datetime(2026, 9, 16, tzinfo=timezone.utc)  # 1 day future
        assert is_plausible(dt, now=now, max_past_days=3, max_future_days=365) is True

    def test_too_far_past_is_not_plausible(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        dt = datetime(2026, 9, 1, tzinfo=timezone.utc)  # 14 days past
        assert is_plausible(dt, now=now, max_past_days=3, max_future_days=365) is False

    def test_too_far_future_is_not_plausible(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        dt = datetime(2028, 9, 15, tzinfo=timezone.utc)  # 2 years future
        assert is_plausible(dt, now=now, max_past_days=3, max_future_days=365) is False

    def test_naive_and_aware_do_not_crash(self):
        """Real bug found via Docker-tested claim logic: mismatched
        naive/aware datetimes used to raise a TypeError on subtraction.
        """
        dt = datetime(2026, 9, 16)  # naive
        result = is_plausible(dt, max_past_days=3, max_future_days=365)
        assert isinstance(result, bool)
