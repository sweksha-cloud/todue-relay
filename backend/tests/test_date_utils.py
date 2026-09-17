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

    def test_spelled_out_region_name_resolved_correctly(self):
        """Confirmed real bug (2026-09-17): the LLM wrote 'Eastern' instead
        of 'EST'/'EDT'. dateutil never even considers 'Eastern' a tz-name
        candidate (tokens over 5 chars are ignored), so it silently fell
        back to the local machine's zone (Pacific) — created a Calendar
        event 3 hours off with no warning.
        """
        dt = parse_deadline_date("September 15, 2026 at 7:00 PM Eastern")
        assert dt.utcoffset().total_seconds() == -4 * 3600  # EDT in September

        dt_winter = parse_deadline_date("January 15, 2026 at 7:00 PM Eastern")
        assert dt_winter.utcoffset().total_seconds() == -5 * 3600  # EST in January

    def test_spelled_out_standard_time_uses_fixed_offset(self):
        dt = parse_deadline_date("September 15, 2026 at 7:00 PM Eastern Standard Time")
        assert dt.utcoffset().total_seconds() == -5 * 3600

    def test_bare_ct_abbreviation_resolved_correctly(self):
        """Confirmed real bug (2026-09-18): a live Calendar event was
        created 5 hours off because bare 'CT' (as opposed to 'CST'/'CDT')
        wasn't in the original abbreviation table and silently fell back
        to the local machine's zone.
        """
        dt = parse_deadline_date("September 24, 2026 at 11:59 p.m. CT")
        assert dt.utcoffset().total_seconds() == -5 * 3600  # CDT in September

    def test_utc_offset_sign_is_not_inverted(self):
        """dateutil resolves 'GMT+2'/'UTC-5' on its own, but backwards —
        POSIX TZ-string convention, where 'GMT+2' means 2 hours *west* of
        UTC, not how anyone actually writes it in an email. Confirmed
        directly: unpatched dateutil parses 'GMT+2' as UTC-02:00.
        """
        dt = parse_deadline_date("September 15, 2026 at 7:00 PM GMT+2")
        assert dt.utcoffset().total_seconds() == 2 * 3600

        dt2 = parse_deadline_date("September 15, 2026 at 7:00 PM UTC-5")
        assert dt2.utcoffset().total_seconds() == -5 * 3600

    def test_utc_offset_with_minutes(self):
        dt = parse_deadline_date("September 15, 2026 at 7:00 PM UTC+5:30")
        assert dt.utcoffset().total_seconds() == 5.5 * 3600

    def test_non_us_named_zones_resolved_correctly(self):
        assert parse_deadline_date(
            "September 15, 2026 at 7:00 PM Central European Time"
        ).utcoffset().total_seconds() == 1 * 3600
        assert parse_deadline_date(
            "September 15, 2026 at 7:00 PM CEST"
        ).utcoffset().total_seconds() == 2 * 3600
        assert parse_deadline_date(
            "September 15, 2026 at 7:00 PM India Standard Time"
        ).utcoffset().total_seconds() == 5.5 * 3600
        assert parse_deadline_date(
            "September 15, 2026 at 7:00 PM Japan Standard Time"
        ).utcoffset().total_seconds() == 9 * 3600

    def test_non_us_multiword_zone_does_not_corrupt_bare_region_match(self):
        """'Central European Time' contains the word 'Central' — make sure
        it's substituted as a whole phrase before the bare US-region
        pattern gets a chance to match just 'Central' out of the middle of
        it and corrupt the rest.
        """
        dt = parse_deadline_date("September 15, 2026 at 7:00 PM Central European Time")
        assert dt.utcoffset().total_seconds() == 1 * 3600  # CET, not US Central

        # bare US "Central" must still resolve correctly on its own
        dt2 = parse_deadline_date("September 15, 2026 at 7:00 PM Central")
        assert dt2.utcoffset().total_seconds() == -5 * 3600  # CDT in September

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
