"""Manual entry point for Step 5's pipeline — also what CI schedules.

Usage (from backend/, with .venv active):
    python -m scripts.run_pipeline             # a real run: spends Gemini quota
    python -m scripts.run_pipeline --dry-run   # report only: spends nothing, writes nothing

--dry-run exists because checking the pipeline shouldn't cost real quota (the
free tier is 20 Gemini calls/day). See claude/tradeoffs/dry-run-mode.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.pipeline import run_pipeline  # noqa: E402


def _print_dry_run_report(result: dict) -> None:
    print("DRY RUN — no Gemini calls, no database writes, no Calendar changes.")
    print(f"  fetched from Gmail:              {result['fetched']}")
    print(f"  already done in a prior run:     {result['already_terminal']}")
    print(f"  rejected by the pre-filter:      {result['filtered_out']}")
    print(f"  would be sent to Gemini:         {result['would_process']}  {result['would_process_ids']}")
    print(f"  deferred (daily budget spent):   {result['deferred']}")
    print(f"  stuck/failed emails to recover:  {len(result['recovery_candidate_ids'])}  {result['recovery_candidate_ids']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would happen; spend no quota, write nothing")
    args = parser.parse_args()

    result = run_pipeline(dry_run=args.dry_run)
    if args.dry_run:
        _print_dry_run_report(result)
    elif result.get("skipped_run"):
        print("Skipped: another pipeline run is in progress. Nothing was changed.")
    else:
        print(f"Done: {result['fetched']} fetched, {result['processed']} processed, {result['failed']} failed.")
        if result["deferred"]:
            print(f"Daily Gemini budget spent: {result['deferred']} email(s) deferred to a later run.")
