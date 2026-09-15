"""Fetch a sample of the real inbox and report how the pre-filter behaves at
each aggressiveness level, so the level can be picked by looking at real
data instead of guessing.

Usage:
    python -m scripts.tune_filter --max-results 200 --query "is:unread newer_than:14d"

Run from backend/ with the venv active. First run will open a browser for
Gmail OAuth consent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.filters import LEVEL_THRESHOLDS, score_email
from app.gmail_client import fetch_recent_messages, get_gmail_service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument(
        "--query",
        type=str,
        default="is:unread newer_than:14d",
        help="Gmail search query. Defaults to matching the real pipeline's scope (unread + recent).",
    )
    parser.add_argument(
        "--show-level",
        choices=list(LEVEL_THRESHOLDS),
        default="moderate",
        help="Print the subjects that pass at this level, for manual eyeballing.",
    )
    args = parser.parse_args()

    service = get_gmail_service()
    print(f"Fetching up to {args.max_results} messages matching {args.query!r}...")
    messages = fetch_recent_messages(service, max_results=args.max_results, query=args.query)
    print(f"Fetched {len(messages)} messages.\n")

    scored = [(m, score_email(m.subject, m.body_text)) for m in messages]

    print("Pass counts by level (higher = more sent to the LLM):")
    for level, threshold in LEVEL_THRESHOLDS.items():
        passed = sum(1 for _, signals in scored if sum(signals.values()) >= threshold)
        pct = 100 * passed / len(scored) if scored else 0
        print(f"  {level:>8} (>= {threshold} signals): {passed:>4} / {len(scored)}  ({pct:.1f}%)")

    print(f"\nSubjects passing at '{args.show_level}' level:")
    threshold = LEVEL_THRESHOLDS[args.show_level]
    for m, signals in scored:
        if sum(signals.values()) >= threshold:
            matched = ", ".join(k for k, v in signals.items() if v)
            print(f"  [{matched}] {m.subject!r}")


if __name__ == "__main__":
    main()
