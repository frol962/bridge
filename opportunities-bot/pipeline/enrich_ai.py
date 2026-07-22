"""
Enrichment runner — the loop that uses the extraction agent.

Flow each run:
  1. Pull rows that are thin (short/empty description OR no year group) and live.
  2. For each, call the extraction agent (reads the real page, returns fields).
  3. MERGE: only fill fields that are currently empty. Never overwrite existing
     good data, never overwrite a human-edited row.
  4. Low-confidence results -> review_status 'pending' + is_live stays as-is,
     so a person checks before it's trusted.
  5. Write back to Supabase.

Cost control: BATCH_LIMIT caps how many pages get read per run, so a runaway
loop can't burn your whole API balance. At ~1 page per few pennies, a 25-row
batch is small change, and stale rows get picked up on later runs.
"""

from __future__ import annotations

import logging
import os

import requests

from pipeline.extract import extract

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TABLE = "opportunities"

BATCH_LIMIT = 25   # pages read per run - keep small, cheap, and predictable

# Fields the extractor may fill. Only filled if the current DB value is empty.
FILLABLE = [
    "description", "age_min", "age_max", "year_groups",
    "length", "location", "cost", "deadline", "eligibility", "apply_url",
]


def _headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


def fetch_thin_rows(limit: int) -> list[dict]:
    """
    Rows worth enriching: live, not human-edited, and missing a description or a
    year group. `enriched_at is null` first so we never re-pay for a row we have
    already read once.
    """
    params = {
        "select": "*",
        "is_live": "eq.true",
        "or": "(description.is.null,year_groups.eq.{})",
        "order": "enriched_at.asc.nullsfirst",
        "limit": str(limit),
    }
    # Skip rows a human has curated by hand, if that column exists.
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(), params=params, timeout=60,
    )
    r.raise_for_status()
    rows = r.json()
    return [row for row in rows if not row.get("is_manual")]


def merge(existing: dict, extracted: dict) -> dict:
    """Fill only empty fields. Existing good data is never overwritten."""
    patch = {}
    for field in FILLABLE:
        new = extracted.get(field)
        if _is_empty(new):
            continue
        if _is_empty(existing.get(field)):
            patch[field] = new
    return patch


def write_back(row_id: str, patch: dict, needs_review: bool, confidence: float) -> None:
    from datetime import datetime, timezone
    patch = dict(patch)
    patch["enriched_at"] = datetime.now(timezone.utc).isoformat()
    patch["extract_confidence"] = confidence
    if needs_review:
        patch["review_status"] = "pending"
        patch["review_reason"] = f"low extraction confidence ({confidence:.2f})"
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers={**_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{row_id}"},
        json=patch,
        timeout=60,
    )
    r.raise_for_status()


def run() -> dict:
    rows = fetch_thin_rows(BATCH_LIMIT)
    log.info("enrich: %s thin rows to process", len(rows))

    filled = reviewed = skipped = 0
    for row in rows:
        result = extract(row)
        if result is None:
            skipped += 1
            continue
        confidence = float(result.get("confidence", 0.0))
        patch = merge(row, result)
        if not patch and not result.get("needs_review"):
            # Nothing new learned and confident it's fine — mark enriched so we
            # don't keep paying to re-read it.
            write_back(row["id"], {}, False, confidence)
            skipped += 1
            continue
        write_back(row["id"], patch, result.get("needs_review", False), confidence)
        filled += 1
        if result.get("needs_review"):
            reviewed += 1

    summary = {"processed": len(rows), "filled": filled,
               "flagged_for_review": reviewed, "skipped": skipped}
    log.info("enrich done: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
