"""How the dashboard groups emails: by the decision that has been made about each one.

Every email the dashboard lists (a deadline, or anything now on the calendar; action items have their own
panel, and removed items are hidden) is in exactly one of these. They are defined once, here, so the
dashboard, and anything else that shows the same lists, cannot disagree about what a category means. The
matching queries are in repository.py (list_category / count_category), and a test checks that the
categories partition the list: nothing in two, nothing in none.

`visible_in_dashboard=False` (marked_correct only) means the category still exists as a query and still
holds the row — the vote still counts toward the correction-rate stat, and the real Calendar event is
untouched — it just isn't rendered as a section on the dashboard: once something is marked correct, the
user does not want to keep seeing it. main.py's dashboard route skips these when building `sections`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    blurb: str
    open_by_default: bool  # starts expanded (the ones that want a decision, or are worth a look)
    show_when_empty: bool  # the two main working sections always show; the rest appear when they have something
    visible_in_dashboard: bool = True  # False: tracked, but never rendered as a dashboard section


CATEGORIES: tuple[Category, ...] = (
    Category(
        "needs_review",
        "Needs your review",
        "Held back: not sure enough to add to your calendar. Approve to add it, reschedule to add it at a time you choose, or deny it.",
        open_by_default=True,
        show_when_empty=True,
    ),
    Category(
        "to_check",
        "On your calendar: to check",
        "Added automatically. Mark it correct or incorrect, reschedule it, or remove it.",
        open_by_default=True,
        show_when_empty=True,
    ),
    Category(
        "marked_incorrect",
        "Marked incorrect",
        "Still on your calendar. Reschedule it to fix the time, or remove it.",
        open_by_default=True,
        show_when_empty=False,
    ),
    Category(
        "failed",
        "Failed",
        "Could not be processed. Retried automatically until it runs out of attempts.",
        open_by_default=True,
        show_when_empty=False,
    ),
    Category(
        "marked_correct",
        "Marked correct",
        "Checked, and right.",
        open_by_default=False,
        show_when_empty=False,
        visible_in_dashboard=False,  # the user does not want to keep seeing something once it's confirmed right
    ),
    Category(
        "in_progress",
        "In progress",
        "Being processed right now.",
        open_by_default=True,
        show_when_empty=False,
    ),
    Category(
        "skipped",
        "Denied or skipped",
        "Not added to your calendar: you denied it, or it was skipped (for example a real calendar invite, "
        "which Gmail already handles).",
        open_by_default=False,
        show_when_empty=False,
    ),
)

CATEGORY_KEYS = tuple(c.key for c in CATEGORIES)
