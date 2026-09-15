"""Date parsing for LLM-extracted deadline strings.

Parseability (is this a real, resolvable date?) is objective and handled
here. *Plausibility* (should we trust a technically-parseable but stale or
absurdly-far-off date?) is a policy decision pending user input — see
claude/tradeoffs/ — so `is_plausible` below takes explicit bounds rather than
hardcoding a default, and nothing calls it yet.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dateutil import tz as date_tz

from app.config import CALENDAR_TIMEZONE

# dateutil doesn't resolve timezone abbreviations on its own (many are
# genuinely ambiguous — "CST" is both US Central and China Standard Time).
# Without this, a naive fallback silently mis-mapped e.g. "5:00 PM EST" to
# the local machine's zone instead of Eastern — a real, confirmed 3-hour
# error found in testing. This covers the common US cases explicitly with
# FIXED offsets matching the literal abbreviation (EST is always -5:00,
# never adjusted for DST — that's what EDT is for), since an abbreviation
# in casual writing means exactly what it says, not "whatever the rule for
# this date happens to be." CST is assumed US Central, the far more likely
# reading for a personal inbox; genuinely unresolvable abbreviations still
# fall through to the local-timezone fallback below.
_US_TZ_OFFSETS = {
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "UTC": 0, "GMT": 0,
}
KNOWN_TZ_ABBREVIATIONS = {
    name: date_tz.tzoffset(name, timedelta(hours=offset))
    for name, offset in _US_TZ_OFFSETS.items()
}


def detect_local_timezone() -> str:
    """IANA timezone name (e.g. "America/New_York") for this machine, read
    from /etc/localtime (macOS/Linux). Shared by the Calendar client (which
    needs it to attach an explicit UTC offset to timed events — see
    calendar_client.py) and the dashboard (which needs it to display
    timestamps in your local time instead of raw UTC).
    """
    if CALENDAR_TIMEZONE:
        return CALENDAR_TIMEZONE
    try:
        resolved = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        idx = resolved.find(marker)
        if idx != -1:
            return resolved[idx + len(marker) :]
    except OSError:
        pass
    return "UTC"


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a UTC-aware datetime (as stored in Postgres) to this
    machine's local timezone for display. None-safe for template use.
    """
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(detect_local_timezone()))


class UnparseableDateError(ValueError):
    """Raised when a string claiming to be a date/time cannot be resolved to one."""


def parse_deadline_date(raw: str | None, *, reference: datetime | None = None) -> datetime | None:
    """Parse an LLM-extracted date/time string into a real datetime.

    Returns None if `raw` is None/empty (the LLM found no date). Raises
    UnparseableDateError if `raw` is present but not a resolvable date —
    callers decide what to do with that (e.g. treat as low confidence).

    `reference` anchors relative/partial dates (e.g. "next Friday", "the
    15th") to a point in time; defaults to now.
    """
    if raw is None or not raw.strip():
        return None

    default = (reference or datetime.now()).replace(second=0, microsecond=0)
    try:
        parsed = date_parser.parse(raw, default=default, fuzzy=True, tzinfos=KNOWN_TZ_ABBREVIATIONS)
    except (date_parser.ParserError, ValueError, OverflowError) as e:
        raise UnparseableDateError(f"Could not parse {raw!r} as a date/time") from e

    if parsed.tzinfo is not None:
        return parsed  # the string named an explicit zone (e.g. "PDT") — trust it

    # No zone in the string: the wall-clock numbers are meant as local time
    # (that's what "4:45 PM" in an email means to a human reading it).
    # Attaching local tzinfo now — rather than leaving this naive — is what
    # makes every downstream consumer (Postgres storage, Calendar event
    # creation, dashboard display) agree on what instant this actually is.
    # Leaving it naive was the root cause of a real bug: a Calendar event
    # got created 7 hours off because the naive value was later treated as
    # UTC by both Postgres and the Calendar API.
    return parsed.replace(tzinfo=ZoneInfo(detect_local_timezone()))


def has_explicit_time(raw: str | None) -> bool:
    """Did the original string encode an actual time, or would parsing it
    just fall back to whatever reference time was supplied?

    Parses twice against two very different reference times — if the
    string carries its own time, both parses agree regardless of the
    reference; if not, each parse just inherits its reference's hour/
    minute. Used to decide all-day vs. timed Calendar events.
    """
    if raw is None or not raw.strip():
        return False

    ref_a = datetime(2000, 1, 1, 0, 0)
    ref_b = datetime(2000, 1, 1, 13, 37)
    try:
        parsed_a = date_parser.parse(raw, default=ref_a, fuzzy=True)
        parsed_b = date_parser.parse(raw, default=ref_b, fuzzy=True)
    except (date_parser.ParserError, ValueError, OverflowError):
        return False

    return (parsed_a.hour, parsed_a.minute) == (parsed_b.hour, parsed_b.minute)


def is_plausible(
    dt: datetime,
    *,
    now: datetime | None = None,
    max_past_days: int,
    max_future_days: int,
) -> bool:
    """Check whether a parsed date falls within an explicit plausible window.

    Bounds are required args, not defaults, because how far past/future is
    "plausible" is a pending policy decision (see claude/tradeoffs/), not something
    to bake in silently.
    """
    # dt may be naive or tz-aware (the LLM can surface a string with an
    # explicit timezone, e.g. "5pm EST") — match now's awareness to dt's so
    # the subtraction below never raises on a naive/aware mismatch.
    now = now or datetime.now(dt.tzinfo)
    delta_days = (dt - now).days
    return -max_past_days <= delta_days <= max_future_days
