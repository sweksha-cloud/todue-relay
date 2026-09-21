"""Cheap keyword/pattern pre-filter, run before any email is sent to the LLM.

Goal: cut the volume of emails that reach the (paid, slower) extraction step
without silently deciding how aggressive that cut should be — see the
tuning script in scripts/tune_filter.py and the note in README-ish form in
the PR/conversation about the strict vs. loose tradeoff.

The filter works by scoring an email against three independent signal
categories. A level then sets how many *distinct categories* must match for
the email to pass:

  - "loose"    -> 1 category required  (misses fewer deadlines, more LLM calls)
  - "moderate" -> 2 categories required (default starting point)
  - "strict"   -> 3 categories required (fewer LLM calls, more risk of missing one)
"""

from __future__ import annotations

import re

from app.config import FILTER_LEVEL

DEADLINE_KEYWORDS = [
    r"\bdue\b",
    r"\bdeadline\b",
    r"\bdue date\b",
    r"\bsubmit(ted)? by\b",
    r"\brsvp\b",
    r"\bexpir(es|ing|ation)\b",
    r"\blast day\b",
    r"\bfinal (notice|reminder|day)\b",
    r"\boverdue\b",
    r"\bno later than\b",
    r"\brespond by\b",
    r"\bpay(ment)? (is )?due\b",
    r"\brenew(al)? by\b",
    r"\bregist(er|ration) (by|deadline)\b",
    r"\bclose[sd]? (on|by)\b",
]

ACTION_VERBS = [
    r"\bsubmit\b",
    r"\bcomplete\b",
    r"\bregister\b",
    r"\bconfirm\b",
    r"\brenew\b",
    r"\bschedule\b",
    r"\bapply\b",
    r"\bpay\b",
]

# Matches explicit calendar dates, relative dates, and weekday references.
DATE_PATTERNS = [
    r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b",  # 4/15, 4/15/2026
    r"\b\d{4}-\d{2}-\d{2}\b",  # 2026-04-15
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?\b",
    r"\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(the\s+)?\d{1,2}(st|nd|rd|th)?\b",
    r"\b(today|tomorrow|tonight|this (week|weekend)|next (week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    r"\bby\s+(noon|midnight|end of day|eod|\d{1,2}\s*(am|pm))\b",
]

_COMPILED_KEYWORDS = re.compile("|".join(DEADLINE_KEYWORDS), re.IGNORECASE)
_COMPILED_ACTIONS = re.compile("|".join(ACTION_VERBS), re.IGNORECASE)
_COMPILED_DATES = re.compile("|".join(DATE_PATTERNS), re.IGNORECASE)

LEVEL_THRESHOLDS = {"loose": 1, "moderate": 2, "strict": 3}


def score_email(subject: str, body_text: str) -> dict:
    """Return which of the three signal categories matched, for a given email."""
    text = f"{subject}\n{body_text}"
    return {
        "keyword": bool(_COMPILED_KEYWORDS.search(text)),
        "action_verb": bool(_COMPILED_ACTIONS.search(text)),
        "date_pattern": bool(_COMPILED_DATES.search(text)),
    }


def is_deadline_candidate(subject: str, body_text: str, level: str = FILTER_LEVEL) -> bool:
    if level not in LEVEL_THRESHOLDS:
        raise ValueError(f"Unknown filter level {level!r}; choose from {list(LEVEL_THRESHOLDS)}")

    signals = score_email(subject, body_text)
    return sum(signals.values()) >= LEVEL_THRESHOLDS[level]


# Unrelated to the Step 1 pre-filter above — used by duplicate-deadline
# detection (docs/design-decisions.md, decision 5) to decide
# whether to search across ALL tracked deadlines (not just the same day)
# for a possible reschedule. Known, accepted gap: a reschedule that just
# restates a new date with none of these words won't be caught this way —
# it'll create a second dashboard entry instead, which is visible, not silent.
RESCHEDULE_KEYWORDS = [
    r"\bresc?hedul(e|ed|ing)\b",
    r"\bpostpon(e|ed|ing)\b",
    r"\bmoved to\b",
    r"\bpush(ed)? back\b",
    r"\bnew (date|deadline|due date)\b",
    r"\bupdated (date|deadline|due date)\b",
    r"\bextend(ed)?\b",
    r"\bchanged? to\b",
    r"\bnow due\b",
]
_COMPILED_RESCHEDULE = re.compile("|".join(RESCHEDULE_KEYWORDS), re.IGNORECASE)


def contains_reschedule_language(subject: str, body_text: str) -> bool:
    return bool(_COMPILED_RESCHEDULE.search(f"{subject}\n{body_text}"))
