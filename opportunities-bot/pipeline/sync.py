"""
Normalise scraped listings and push them to Supabase.

Two rules that matter more than they look:

1. Never DELETE a row that vanished from the source. A source going quiet for one
   run (WAF hiccup, deploy, outage) would wipe your site. Mark it and let it age
   out instead.

2. A run that returns zero rows is a FAILURE, not an empty result. Silent zero is
   how aggregators die - the cron goes green every 6 hours while the site slowly
   empties. We raise instead.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # server-side only. Never ships to the browser.
TABLE = "opportunities"

# If a run yields fewer than this, something broke upstream. Tune once you know
# your real baseline.
MIN_EXPECTED_ROWS = 5

SUBJECT_KEYWORDS = {
    "finance": ["finance", "banking", "investment", "audit", "tax", "accountancy", "insurance"],
    "business": ["business", "consulting", "professional services", "marketing", "commerce"],
    "economics": ["economics", "economic", "markets"],
    "law": ["law", "legal", "solicitor", "compliance"],
    "politics": ["politics", "policy", "parliament", "government", "civil service"],
    "psychology": ["psychology", "mental health", "wellbeing"],
    "media": ["media", "journalism", "communications", "broadcast"],
}

CATEGORY_KEYWORDS = [
    ("volunteering", ["volunteer", "volunteering", "cadet"]),
    ("internship", ["internship", "intern", "placement", "apprenticeship"]),
    ("work_experience", ["work experience", "insight", "taster", "shadow", "masterclass"]),
]


def categorise(title: str, description: str) -> str:
    hay = f"{title} {description}".lower()
    for category, words in CATEGORY_KEYWORDS:
        if any(w in hay for w in words):
            return category
    return "work_experience"


def tag_subjects(title: str, description: str, organisation: str) -> list[str]:
    hay = f"{title} {description} {organisation}".lower()
    hits = [s for s, words in SUBJECT_KEYWORDS.items() if any(w in hay for w in words)]
    return hits or ["general"]


def derive_status(start_date: str | None) -> str:
    """A listing whose start date has passed is closed, whatever the source says."""
    if not start_date:
        return "open"
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return "open"
    return "closed" if start < date.today() else "open"


def guess_location(title: str, raw_location: str | None) -> str | None:
    if raw_location:
        return raw_location
    # Uptree titles are reliably "City: Employer, Thing" or "Employer: Thing, City"
    m = re.match(r"^([A-Z][A-Za-z\s]{2,20}):\s", title)
    if m and m.group(1) not in ("National",):
        return m.group(1).strip()
    return None


def normalise(raw: dict) -> dict:
    title = raw.get("title") or ""
    description = raw.get("description") or ""
    organisation = raw.get("organisation") or ""

    return {
        "id": raw["source_id"],
        "title": title,
        "organisation": organisation,
        "category": categorise(title, description),
        "subjects": tag_subjects(title, description, organisation),
        "description": description or None,
        "location": guess_location(title, raw.get("location")),
        "country": "UK",
        "start_date": raw.get("start_date"),
        "end_date": raw.get("end_date"),
        "status": derive_status(raw.get("start_date")),
        "url": raw["url"],
        "apply_url": raw.get("apply_url"),
        "link_type": raw.get("link_type", "portal_apply"),
        "logo_url": raw.get("logo_url"),
        "source": raw["source"],
        "verified_on": date.today().isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "is_live": True,
    }


def _headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=id",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows,
        timeout=60,
    )
    r.raise_for_status()
    return len(rows)


def expire_stale(days: int = 14) -> None:
    """
    Anything we haven't seen in `days` gets hidden, not deleted. If a source comes
    back we just start seeing it again and is_live flips back on its own.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        params={"last_seen_at": f"lt.{cutoff}", "is_live": "is.true"},
        headers={**_headers(), "Prefer": "return=minimal"},
        json={"is_live": False},
        timeout=60,
    )
    r.raise_for_status()
    log.info("expired listings unseen since %s", cutoff[:10])


def run(scraped: list[dict]) -> int:
    if len(scraped) < MIN_EXPECTED_ROWS:
        raise RuntimeError(
            f"Only {len(scraped)} rows scraped (expected >= {MIN_EXPECTED_ROWS}). "
            "Refusing to sync - the source layout or your access probably broke."
        )
    rows = [normalise(r) for r in scraped]
    seen = {r["id"] for r in rows}
    if len(seen) != len(rows):
        log.warning("dropped %s duplicate ids", len(rows) - len(seen))
        rows = list({r["id"]: r for r in rows}.values())

    count = upsert(rows)
    expire_stale()
    log.info("synced %s listings", count)
    return count
