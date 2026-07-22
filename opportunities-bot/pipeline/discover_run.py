"""
Discovery runner. Loops SOURCES (pipeline/discover.py), writes any found
opportunity to Supabase. New rows never collide with existing ones because
their id is derived from the source URL (see _make_id).
"""

from __future__ import annotations

import logging
import os

import requests

from pipeline.discover import SOURCES, discover_one

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TABLE = "opportunities"


def _headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def upsert(row: dict) -> None:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=id",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=[row],
        timeout=60,
    )
    r.raise_for_status()


def run() -> dict:
    found = live = review = skipped = 0
    for source in SOURCES:
        row = discover_one(source)
        if row is None:
            skipped += 1
            continue
        upsert(row)
        found += 1
        if row["is_live"]:
            live += 1
        elif row["review_status"] == "pending":
            review += 1

    summary = {"sources_checked": len(SOURCES), "opportunities_found": found,
               "published_live": live, "sent_to_review": review, "no_result": skipped}
    log.info("discover done: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
