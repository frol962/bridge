"""
Discovery agent.

Different job from extract.py: that one improves a row that ALREADY EXISTS.
This one reads a page and asks "is there a real, current opportunity here at
all", and if so, creates a brand new row.

Same core idea as the extractor - one LLM prompt reads many different page
layouts, instead of writing one parser per site. That's what makes this scale:
adding a source is adding one line to SOURCES, not writing new code.

SOURCES is deliberately short and hand-picked rather than "search the whole
web". Every entry is a page I have verified is real, plain HTML (no login,
no JavaScript-rendered content) and actually describes a live opportunity.
Growing this list is the main lever for growing the site - see the comment
above SOURCES for how to add one.

Safety rules, same spirit as extract.py:
  - Never invent a fact. Null over guess.
  - Confidence score. Anything below the bar -> review queue, not published.
  - A source producing nothing usable is skipped, not treated as an error -
    a competition page between cycles legitimately has nothing to find.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
from datetime import date, datetime, timezone

import anthropic
import requests
from bs4 import BeautifulSoup

from pipeline.relevance import assess

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-6"
PAGE_CHAR_LIMIT = 12000
CONFIDENCE_BAR = 0.6

FETCH_HEADERS = {
    "User-Agent": "BridgeOpportunitiesBot/1.0 (+https://bridgeopportunities.lovable.app)",
    "Accept": "text/html,application/xhtml+xml",
}

# ---------------------------------------------------------------------------
# SOURCES - the list that actually grows the site.
#
# To add one: find the organisation's OWN page for the opportunity (not an
# aggregator - aggregators are often JS-rendered or behind logins), confirm
# it loads as plain HTML by pasting the URL into a browser and viewing source,
# then add one line here. That's the whole process.
# ---------------------------------------------------------------------------
SOURCES = [
    # -- Competitions --
    {"url": "https://blueoceancompetition.org/", "category": "competition"},
    {"url": "https://www.johnlockeinstitute.com/essay-competition", "category": "competition"},
    {"url": "https://www.discovereconomics.co.uk/young-economist-of-the-year-2026", "category": "competition"},
    {"url": "https://www.bpho.org.uk/", "category": "competition"},

    # -- Summer schools (UK/Europe/top US) --
    {"url": "https://www.uniq.ox.ac.uk/", "category": "summer_school"},
    {"url": "https://debatechamber.com/project/ppe-summer-school/", "category": "summer_school"},
    {"url": "https://globalyouth.wharton.upenn.edu/", "category": "summer_school"},
    {"url": "https://globalscholars.yale.edu/", "category": "summer_school"},
    {"url": "https://www.suttontrust.com/our-programmes/summer-schools/", "category": "summer_school"},

    # -- Volunteering (London & Kent only, per RULES.md). National charities
    #    with LOCAL branches are included - the discovery agent records the
    #    real location it finds (or marks it for review if the page is a
    #    national hub with no specific branch named), rather than guessing. --
    {"url": "https://youthjoining.sja.org.uk/", "category": "volunteering"},
    {"url": "https://royalfreecharity.org/get-involved/volunteer/young-volunteers-programme",
     "category": "volunteering"},
    {"url": "https://www.oxfam.org.uk/get-involved/volunteer-with-us/volunteer-in-an-oxfam-shop/",
     "category": "volunteering"},
    {"url": "https://www.trussell.org.uk/support-us/volunteer", "category": "volunteering"},
    {"url": "https://www.redcross.org.uk/get-involved/volunteer-with-us", "category": "volunteering"},
    {"url": "https://www.bhf.org.uk/how-you-can-help/volunteer/volunteering-for-young-people",
     "category": "volunteering"},

    # -- Employers' OWN pages (not Uptree). Some large employers route their
    #    own "Apply Now" through Uptree/Bright Network as their booking system -
    #    that's how their recruitment is actually built, not a shortcut I took.
    #    Where that happens the discovery agent still records THIS page as the
    #    source and pulls whatever direct link it finds. --
    {"url": "https://careers.jpmorgan.com/us/en/students/programs/uk-work-experience",
     "category": "work_experience"},
    {"url": "https://www2.deloitte.com/uk/en/careers/students/aspire.html", "category": "internship"},
    {"url": "https://home.kpmg/uk/en/home/careers/students.html", "category": "internship"},
    {"url": "https://www.pwc.co.uk/careers/student-careers.html", "category": "internship"},
    {"url": "https://www.civil-service-careers.gov.uk/networks-and-support/social-mobility/",
     "category": "internship"},

    # -- Social mobility / access programmes (national, structured, real) --
    {"url": "https://www.socialmobility.org.uk/programme", "category": "internship"},
    {"url": "https://www.suttontrust.com/our-programmes/pathways-to-law/", "category": "internship"},
    {"url": "https://www.suttontrust.com/our-programmes/pathways-to-banking-and-finance/",
     "category": "internship"},

    # -- More competitions --
    {"url": "https://www.economics-observatory.com/young-economist-of-the-year", "category": "competition"},
    {"url": "https://www.rgs.org/schools/teaching-resources/young-geographer-of-the-year/",
     "category": "competition"},

    # -- More summer schools --
    {"url": "https://www.lse.ac.uk/study-at-lse/summer-schools", "category": "summer_school"},
    {"url": "https://www.imperial.ac.uk/be-inspired/schools-outreach/secondary-schools/summer-schools/",
     "category": "summer_school"},

    # -- Employers who host applications on THEIR OWN site (genuinely no
    #    third-party signup needed - the whole point the user asked for). --
    {"url": "https://barclayslifeskills.com/i-want-virtual-work-experience/school/online-work-experience/",
     "category": "online_course"},
    {"url": "https://barclayslifeskills.com/help-others/lessons/finding-work-experience/",
     "category": "online_course"},
]

DISCOVERY_PROMPT = """You are looking at a web page to find ONE real, current, applyable opportunity for UK/European students in school Years 11-13 (ages ~15-18), for a site called Bridge that lists internships, work experience, summer schools, volunteering, competitions, insight days and online courses in these subjects: business, economics, finance, politics, philosophy, psychology, law, media.

Return ONLY a JSON object, no prose, no markdown fences:

{{
  "found": boolean,               // true only if this page describes ONE specific, currently-relevant opportunity
  "title": string | null,
  "organisation": string | null,
  "description": string | null,   // 2-4 sentences, your own words, what the student actually does
  "age_min": integer | null,
  "age_max": integer | null,
  "year_groups": string[],
  "length": string | null,
  "location": string | null,      // city, "Online", or "Various"
  "cost": string | null,
  "deadline": string | null,      // ISO date if stated
  "eligibility": string | null,
  "apply_url": string | null,     // the direct application link if visible
  "confidence": number            // 0.0-1.0
}}

Rules:
- found=false if: the page is between cycles with no live opportunity, is a general homepage with no specific programme details, requires login to see real content, or is not really for this age group/region.
- NEVER invent a fact not stated on the page. Null over guess.
- description must be prose, never a copied label like "Location: X" or "Date: Y".
- Do not start every description the same way - vary sentence openings naturally.
- confidence reflects how sure you are this is a real, current, applyable opportunity - not how well-known the organisation is.

SOURCE URL: {url}
CATEGORY HINT: {category}
PAGE CONTENT:
{content}
"""


def _fetch_text(url: str) -> str | None:
    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=25)
        if r.status_code != 200:
            log.info("discover: %s returned %s", url, r.status_code)
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "svg", "header"]):
            tag.decompose()
        text = html.unescape(soup.get_text("\n", strip=True))
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:PAGE_CHAR_LIMIT] or None
    except requests.RequestException as e:
        log.warning("discover: fetch failed %s: %s", url, e)
        return None


def _parse_json(raw: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _derive_status(deadline: str | None) -> str:
    """
    A competition/summer school whose deadline has passed is closed - however
    good the page looked to the model. This was previously hardcoded to
    "open" regardless of date, which is a real bug: it let a competition that
    closed weeks ago show as currently open.
    """
    if not deadline:
        return "open"
    try:
        d = date.fromisoformat(deadline)
    except (ValueError, TypeError):
        return "open"
    return "closed" if d < date.today() else "open"


def _make_id(url: str) -> str:
    """Stable id from the URL, so re-running never creates a duplicate row."""
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    return f"discovered:{digest}"


def discover_one(source: dict) -> dict | None:
    content = _fetch_text(source["url"])
    if not content:
        return None

    prompt = DISCOVERY_PROMPT.format(
        url=source["url"], category=source["category"], content=content
    )
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        log.warning("discover: API error for %s: %s", source["url"], e)
        return None

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _parse_json(raw)
    if not data or "confidence" not in data:
        return None
    if not data.get("found"):
        log.info("discover: nothing found at %s", source["url"])
        return None

    row = {
        "id": _make_id(source["url"]),
        "title": data.get("title") or "",
        "organisation": data.get("organisation") or "",
        "category": source["category"],
        "description": data.get("description"),
        "location": data.get("location"),
        "country": "UK",
        "year_groups": data.get("year_groups") or [],
        "age_min": data.get("age_min"),
        "age_max": data.get("age_max"),
        "length": data.get("length"),
        "cost": data.get("cost"),
        "deadline": data.get("deadline"),
        "eligibility": data.get("eligibility"),
        "url": source["url"],
        "apply_url": data.get("apply_url") or source["url"],
        "link_type": "direct_apply" if data.get("apply_url") else "programme_page",
        "source": "discovery",
        "verified_on": date.today().isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "extract_confidence": float(data["confidence"]),
    }

    verdict = assess({
        "title": row["title"], "description": row["description"] or "",
        "organisation": row["organisation"], "location": row["location"] or "",
        "category": row["category"],
    })
    low_confidence = float(data["confidence"]) < CONFIDENCE_BAR
    row["subjects"] = verdict["subjects"] or ["general"]
    row["is_live"] = verdict["decision"] == "show" and not low_confidence
    row["review_status"] = "pending" if (verdict["decision"] == "review" or low_confidence) else None
    row["review_reason"] = verdict["reason"] if row["review_status"] else None
    row["status"] = _derive_status(row.get("deadline"))

    return row
