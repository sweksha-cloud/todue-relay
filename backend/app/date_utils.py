"""Date parsing for LLM-extracted deadline strings.

Parseability (is this a real, resolvable date?) is objective and handled
here. *Plausibility* (should we trust a technically-parseable but stale or
absurdly-far-off date?) is a policy decision, made 2026-09-14 (see
claude/tradeoffs/stale-implausible-date-handling.md) — so `is_plausible`
below takes explicit bounds rather than hardcoding them; the pipeline passes
PLAUSIBLE_MAX_PAST_DAYS / PLAUSIBLE_MAX_FUTURE_DAYS from app/config.py.
"""

from __future__ import annotations

import os
import re
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

# Non-US zones common enough in a personal inbox to be worth naming
# explicitly, same fixed-offset philosophy as the US table above. IST/JST
# don't observe DST at all, so a single fixed entry is correct for either.
_NON_US_TZ_OFFSETS = {
    "CET": 1, "CEST": 2,  # Central European (Summer) Time
    "BST": 1,  # British Summer Time — the DST name; the UK just says "GMT" otherwise
    "IST": 5.5,  # India Standard Time
    "JST": 9,  # Japan Standard Time
}

# Full names for the non-US zones above — same reasoning as
# _SPELLED_TZ_STANDARD_RE etc.: dateutil won't tokenize anything longer
# than 5 chars as a tz-name candidate. Longest/most-specific phrases must
# be substituted before _SPELLED_TZ_PLAIN_RE below runs, or e.g. "Central
# European Time" would have its "Central" alone wrongly swapped for the
# US-Central token first, corrupting the rest of the phrase.
_SPELLED_NON_US_TZ_SUBS = [
    (re.compile(r"\bCentral European Summer Time\b", re.IGNORECASE), "CEST"),
    (re.compile(r"\bCentral European Time\b", re.IGNORECASE), "CET"),
    (re.compile(r"\bBritish Summer Time\b", re.IGNORECASE), "BST"),
    (re.compile(r"\bGreenwich Mean Time\b", re.IGNORECASE), "GMT"),
    (re.compile(r"\bIndia Standard Time\b", re.IGNORECASE), "IST"),
    (re.compile(r"\bJapan Standard Time\b", re.IGNORECASE), "JST"),
]

# dateutil resolves a bare "GMT+2"/"UTC-5" on its own, but with the sign
# inverted from how literally everyone writes it in casual English — it
# follows the POSIX TZ-string convention, where "GMT+2" actually means 2
# hours *west* of UTC. Confirmed directly: dateutil parses "GMT+2" as
# UTC-02:00. That's a silent, doubled error (wrong direction *and* the
# magnitude is compounded once you convert), worse than not resolving it at
# all. Intercepted below before dateutil ever sees the raw offset syntax,
# using civil convention (GMT+2 means 2 hours ahead of UTC) instead.
_UTC_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", re.IGNORECASE)

# Region names ("Eastern", "Pacific") written out in full instead of an
# abbreviation — confirmed real case: "September 15, 2026 at 7:00 PM
# Eastern" silently fell back to the local machine's zone, a real 3-hour
# error, because dateutil's tokenizer only ever considers tokens of <=5
# chars as tz-name candidates ("EASTERN" is too long to even be tried) —
# adding it to KNOWN_TZ_ABBREVIATIONS below wouldn't help on its own.
# Normalized to a short DST-aware token ("ET") before parsing instead; that
# token maps to a real IANA zone (below), not a fixed offset, since a bare
# "Eastern"/"Eastern Time" doesn't say Standard or Daylight the way "EST"
# does — the actual UTC offset depends on the date. An explicit "Eastern
# Standard Time"/"Eastern Daylight Time" is normalized to EST/EDT instead,
# since that phrasing does commit to one.
_TZ_REGION_ABBR_PREFIX = {
    "eastern": "E",
    "central": "C",
    "mountain": "M",
    "pacific": "P",
}
_TZ_REGION_IANA_ZONE = {
    "eastern": "America/New_York",
    "central": "America/Chicago",
    "mountain": "America/Denver",
    "pacific": "America/Los_Angeles",
}
_SPELLED_TZ_STANDARD_RE = re.compile(
    r"\b(Eastern|Central|Mountain|Pacific)\s+Standard\s+Time\b", re.IGNORECASE
)
_SPELLED_TZ_DAYLIGHT_RE = re.compile(
    r"\b(Eastern|Central|Mountain|Pacific)\s+Daylight\s+Time\b", re.IGNORECASE
)
_SPELLED_TZ_PLAIN_RE = re.compile(
    r"\b(Eastern|Central|Mountain|Pacific)(?:\s+Time)?\b", re.IGNORECASE
)


def _normalize_timezone_text(raw: str) -> tuple[str, dict[str, date_tz.tzoffset]]:
    """Rewrite anything dateutil can't tokenize or gets wrong on its own —
    full region/zone names (too long to be considered a tz-name candidate
    at all, see _TZ_REGION_ABBR_PREFIX) and explicit numeric UTC/GMT
    offsets (tokenized fine, but with the sign inverted, see
    _UTC_OFFSET_RE) — into short synthetic tokens dateutil will resolve
    correctly via tzinfos.

    Order matters: numeric offsets and multi-word non-US names are
    substituted first, since they're the most specific; only then the bare
    US region names, so e.g. "Central European Time" is already gone by
    the time the bare "Central" pattern would otherwise wrongly match it.

    Returns the rewritten string plus any per-call tzinfos entries the
    numeric-offset substitution needed (the offset value varies per match,
    so those can't live in the static KNOWN_TZ_ABBREVIATIONS table).
    """
    extra_tzinfos: dict[str, date_tz.tzoffset] = {}

    def offset_sub(m: re.Match) -> str:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        offset = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            offset = -offset
        token = f"ZOF{chr(ord('A') + len(extra_tzinfos))}"
        extra_tzinfos[token] = date_tz.tzoffset(token, offset)
        return token

    raw = _UTC_OFFSET_RE.sub(offset_sub, raw)

    for pattern, abbr in _SPELLED_NON_US_TZ_SUBS:
        raw = pattern.sub(abbr, raw)

    raw = _SPELLED_TZ_STANDARD_RE.sub(
        lambda m: _TZ_REGION_ABBR_PREFIX[m.group(1).lower()] + "ST", raw
    )
    raw = _SPELLED_TZ_DAYLIGHT_RE.sub(
        lambda m: _TZ_REGION_ABBR_PREFIX[m.group(1).lower()] + "DT", raw
    )
    raw = _SPELLED_TZ_PLAIN_RE.sub(
        lambda m: _TZ_REGION_ABBR_PREFIX[m.group(1).lower()] + "T", raw
    )
    return raw, extra_tzinfos


KNOWN_TZ_ABBREVIATIONS = {
    name: date_tz.tzoffset(name, timedelta(hours=offset))
    for name, offset in {**_US_TZ_OFFSETS, **_NON_US_TZ_OFFSETS}.items()
}
# The ambiguous-by-design tokens _normalize_timezone_text() produces
# for a bare region name ("Eastern" / "Eastern Time") — DST-aware real IANA
# zones, not fixed offsets, since unlike "EST" these never committed to
# Standard or Daylight in the first place.
KNOWN_TZ_ABBREVIATIONS.update(
    {
        prefix + "T": ZoneInfo(zone)
        for region, prefix in _TZ_REGION_ABBR_PREFIX.items()
        for zone in [_TZ_REGION_IANA_ZONE[region]]
    }
)


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
    normalized, extra_tzinfos = _normalize_timezone_text(raw)
    tzinfos = {**KNOWN_TZ_ABBREVIATIONS, **extra_tzinfos} if extra_tzinfos else KNOWN_TZ_ABBREVIATIONS
    try:
        parsed = date_parser.parse(normalized, default=default, fuzzy=True, tzinfos=tzinfos)
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

    Bounds are required args, not defaults, so the policy lives in one place
    (app/config.py) instead of being baked in here — see
    claude/tradeoffs/stale-implausible-date-handling.md.
    """
    # dt may be naive or tz-aware (the LLM can surface a string with an
    # explicit timezone, e.g. "5pm EST") — match now's awareness to dt's so
    # the subtraction below never raises on a naive/aware mismatch.
    now = now or datetime.now(dt.tzinfo)
    delta_days = (dt - now).days
    return -max_past_days <= delta_days <= max_future_days
