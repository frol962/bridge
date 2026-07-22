"""
Extraction agent.

Job: take one opportunity whose card is thin (empty description, no age, no
length) and READ its real page — the employer's own page where possible, else
the Uptree page — then return clean structured fields.

Why an LLM and not a parser: every employer site has a different layout. A
parser would be 30 brittle scripts. A model reads them all with one prompt.

Safety rails baked in, because a model that writes to a live site for 16-year-olds
must not invent facts:
  - It is told to return null for anything not stated on the page. Never guess.
  - It returns a confidence score. Low confidence -> the row is flagged for
    review, not silently published.
  - Output is strict JSON, validated before it touches the database. A malformed
    or off-schema reply is discarded, not written.
  - It never overwrites a human-edited field (is_manual rows are skipped upstream).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re

import anthropic
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"
PAGE_CHAR_LIMIT = 12000          # trim huge pages; the useful part is near the top
CONFIDENCE_REVIEW_THRESHOLD = 0.6  # below this -> send to human review

FETCH_HEADERS = {
    "User-Agent": "BridgeOpportunitiesBot/1.0 (+https://bridgeopportunities.lovable.app)",
    "Accept": "text/html,application/xhtml+xml",
}

# The exact shape we ask the model to fill. Definitions matter more than the
# field names - they are how the model knows what "length" or "cost" means.
EXTRACTION_PROMPT = """You are extracting facts about a student opportunity from the web page below, for a UK site that lists work experience, internships, summer schools, volunteering, competitions, insight days and online courses for students in school Years 11-13 (ages ~15-18).

Return ONLY a JSON object, no prose, no markdown fences. Use this exact schema:

{{
  "description": string | null,      // 2-4 plain sentences: what the student actually DOES. No marketing fluff.
  "age_min": integer | null,         // minimum age, only if the page states it
  "age_max": integer | null,         // maximum age, only if the page states it
  "year_groups": string[],           // e.g. ["Year 12","Year 13"]; [] if not derivable
  "length": string | null,           // e.g. "3 days", "2 weeks", "10 hours", "ongoing"
  "location": string | null,         // city/region, or "Online", or "Various"
  "cost": string | null,             // e.g. "Free", "£7,300", "Paid - bursaries available"
  "deadline": string | null,         // ISO date YYYY-MM-DD if a clear deadline is stated, else null
  "eligibility": string | null,      // one sentence on who can apply
  "apply_url": string | null,        // the direct application/registration link if visible on the page
  "confidence": number               // 0.0-1.0: how confident YOU are this is accurate and this is a real, current opportunity
}}

Hard rules:
- If the page does NOT state something, use null (or [] for year_groups). NEVER invent, estimate, or infer a fact that isn't on the page.
- age 14-16 -> ["Year 10","Year 11"]; 16-18 -> ["Year 12","Year 13"]; if the page names year groups directly, use those.
- If the page is an error, a login wall, a generic careers homepage, or clearly not this specific opportunity, set confidence below 0.4 and null everything you can't verify.
- description must be about THIS opportunity, in your own words, under 60 words.
- description must be PROSE - complete sentences describing what the student does.
  NEVER copy label fragments from the page. These are all INVALID descriptions:
    "Location: Virtual event hosted on Zoom."
    "Location: Cambridge, Edinburgh"
    "Start Date: September 2026"
  If the page gives you no real prose about the activity, set description to null
  and set confidence below 0.5. A null description is far better than a fragment.
- Never include HTML entities (&nbsp; &amp; &#39;) or raw markup in any field.

PAGE TITLE: {title}
ORGANISATION: {organisation}
PAGE CONTENT:
{content}
"""


def _fetch_text(url: str) -> str | None:
    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=25)
        if r.status_code != 200:
            log.info("extract: %s returned %s", url, r.status_code)
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "svg", "header"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        # Decode &nbsp; &amp; etc BEFORE the model sees them, or they end up
        # copied verbatim into descriptions.
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:PAGE_CHAR_LIMIT] or None
    except requests.RequestException as e:
        log.warning("extract: fetch failed %s: %s", url, e)
        return None


def _parse_json(raw: str) -> dict | None:
    """Model should return bare JSON; strip fences defensively and parse."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.M).strip()
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _valid(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if "confidence" not in data:
        return False
    try:
        c = float(data["confidence"])
    except (TypeError, ValueError):
        return False
    return 0.0 <= c <= 1.0


def extract(opportunity: dict) -> dict | None:
    """
    Reads the best available page for this opportunity and returns a dict of
    fields to update, plus 'confidence' and 'needs_review'. Returns None if the
    page couldn't be read or the model's reply was unusable - caller then leaves
    the existing row untouched.
    """
    # Prefer the employer's own apply page; fall back to the listing URL.
    url = opportunity.get("apply_url") or opportunity.get("url")
    if not url:
        return None

    content = _fetch_text(url)
    if not content:
        return None

    prompt = EXTRACTION_PROMPT.format(
        title=opportunity.get("title", ""),
        organisation=opportunity.get("organisation", ""),
        content=content,
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        log.warning("extract: API error for %s: %s", opportunity.get("id"), e)
        return None

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _parse_json(raw)
    if not data or not _valid(data):
        log.info("extract: unusable reply for %s", opportunity.get("id"))
        return None

    confidence = float(data["confidence"])
    data["needs_review"] = confidence < CONFIDENCE_REVIEW_THRESHOLD

    # Never let the model blank out a field that already has a good value; only
    # fill gaps. (The caller merges; this just marks what's usable.)
    return data
