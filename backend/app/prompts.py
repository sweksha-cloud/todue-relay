"""Prompt for the extraction step. Provider-agnostic — whichever LLM is
chosen, this text plus EXTRACTION_JSON_SCHEMA (app/schemas.py) as its
structured-output constraint should be sufficient to wire up a client.
"""

from __future__ import annotations

from app.gmail_client import EmailMessage

EXTRACTION_SYSTEM_PROMPT = """\
You extract deadline and action information from a single email. The email \
has already been pre-filtered as a likely candidate — it may still turn out \
to have no real deadline, which is fine.

Return a JSON object with exactly these fields:
- email_id: the id given below, unchanged
- event_name: a short human-readable name for what this is about \
(e.g. "Assignment 3 submission", "Club dues payment", "Interview scheduling")
- deadline_date: the deadline date/time as it would naturally be written \
(e.g. "April 18, 2026" or "next Friday at 5pm"), or null if there is no \
fixed date
- source_context: one short sentence on what this is for, quoting or \
paraphrasing the relevant part of the email
- confidence: "high" if the email clearly and unambiguously states a \
specific deadline, "low" if you're inferring, guessing, or the email is \
ambiguous about whether there's a real deadline at all
- action_type: one of:
  - "deadline": there's a real, fixed date/time by which something must \
happen (a due date, an RSVP-by date, a payment due date, etc.)
  - "needs_reply": the email is asking the recipient to respond with \
something (propose interview times, confirm attendance, answer a question) \
and does NOT state a fixed date/time of its own — the recipient's reply is \
what's needed, not showing up by a deadline
  - "unclear": genuinely ambiguous whether this needs any action at all
- is_recurring: true only if the email itself describes something that \
repeats on a schedule ("rent due on the 1st of every month", "standup every \
Monday at 9am", "due weekly"), not just because this looks like a deadline \
you've seen before. false for a one-time deadline, and false for \
action_type values other than "deadline".
- recurrence_rule: when is_recurring is true, an iCalendar RRULE value (no \
"RRULE:" prefix), otherwise null. Use only FREQ/BYDAY/BYMONTHDAY/INTERVAL — \
examples: "every Monday" -> "FREQ=WEEKLY;BYDAY=MO", "1st of every month" -> \
"FREQ=MONTHLY;BYMONTHDAY=1", "every day" -> "FREQ=DAILY", "annually" -> \
"FREQ=YEARLY", "every two weeks" -> "FREQ=WEEKLY;INTERVAL=2". deadline_date \
should still be the next upcoming occurrence, as a normal date/time.

If action_type is "needs_reply" or "unclear", deadline_date MUST be null, \
confidence MUST be "low", and is_recurring MUST be false — no calendar \
event will be created; it's handled as an action item to review instead.
"""


def build_extraction_prompt(email: EmailMessage) -> str:
    return (
        f"email_id: {email.id}\n"
        f"From: {email.sender}\n"
        f"Date received: {email.date}\n"
        f"Subject: {email.subject}\n"
        f"Body:\n{email.body_text}\n"
    )
