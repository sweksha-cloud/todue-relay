"""Manual entry point for Step 5's pipeline — run once by hand tonight;
Step 8 (still open, see claude/review.md) decides how this gets scheduled
for real later.

Usage (from backend/, with .venv active):
    python -m scripts.run_pipeline
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.pipeline import run_pipeline  # noqa: E402

if __name__ == "__main__":
    result = run_pipeline()
    print(f"Done: {result['fetched']} fetched, {result['processed']} processed, {result['failed']} failed.")
