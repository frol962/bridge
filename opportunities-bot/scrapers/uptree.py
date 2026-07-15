"""
Uptree scraper.

Design note: this deliberately does NOT depend on CSS class names, which change
without warning and are the usual reason a scraper dies silently. It leans on two
things Uptree is structurally committed to:

  1. URL shape   -> /events/<company-slug>/<numeric-id>/
                    /opportunities/<company-slug>/<numeric-id>/
  2. OpenGraph   -> every detail page ships og:title, og:description, og:url,
                    og:image. They need these for link previews on social, so
                    they are far stickier than markup.

Events  = Uptree-hosted registration (JPMorganChase, Lloyds, Winston Taylor).
Placements = usually link out to the employer's own ATS. We extract that and skip
the middleman.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://uptree.co"
SOURCE = "uptree.co"

INDEXES = [("/events/", "event"), ("/opportunities/", "placement")]

DETAIL_RE = re.compile(r"^/(events|opportunities)/([a-z0-9\-]+)/(\d+)/$")

# og:title on an event page looks like:
#   "Winston Taylor: Tomorrow Talent Work Experience Programme (Paid)
#    - Event on  Mon, August 10, 2026"
EVENT_TITLE_RE = re.compile(r"^(?P<title>.+?)\s+-\s+Event on\s+(?P<when>.+?)$", re.S)
PLACEMENT_TITLE_RE = re.compile(r"^(?P<title>.+?)\s+-\s+Jobs and placements$", re.S)

# "Dates: 3rd - 5th August 2026"  /  "Date: Mon 10th Aug 9:30AM - Fri 21st Aug 5:00PM"
DATE_LINE_RE = re.compile(r"\bDates?:\s*(?P<value>[^\n\r]{4,120})", re.I)

SOCIAL = ("linkedin.com", "instagram.com", "facebook.com", "twitter.com", "x.com")

HEADERS = {
    # Identify the bot honestly and give them a way to reach you. Costs nothing
    # and is the difference between "unknown scraper" and "we'll email first".
    "User-Agent": (
        "OpportunitiesBot/1.0 (+https://YOUR-DOMAIN.com/about; contact@YOUR-DOMAIN.com)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

DELAY_SECONDS = 2.0  # be a good citizen; this is someone else's server
MAX_PAGES = 25  # circuit breaker against a pagination bug looping forever


@dataclass
class RawListing:
    source: str
    source_id: str
    kind: str
    url: str
    title: str | None = None
    organisation: str | None = None
    description: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    apply_url: str | None = None
    link_type: str = "direct_apply"
    logo_url: str | None = None
    scraped_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z"
    )


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session: requests.Session, url: str, attempts: int = 3) -> str | None:
    for i in range(attempts):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                log.info("404 %s", url)
                return None
            if r.status_code == 403:
                # Their WAF has noticed us. Back off rather than hammer.
                log.warning("403 %s - blocked, backing off", url)
                return None
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            wait = 2 ** i
            log.warning("fetch failed (%s/%s) %s: %s", i + 1, attempts, url, e)
            time.sleep(wait)
    return None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    val = tag.get("content") if tag else None
    return val.strip() if val and val.strip() else None


def discover(session: requests.Session) -> list[tuple[str, str]]:
    """Walk the index pages and collect every (detail_url, kind)."""
    found: dict[str, str] = {}

    for path, kind in INDEXES:
        seen_on_previous_pages = -1
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE}{path}?page={page}"
            html = _get(session, url)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            page_urls = set()
            for a in soup.find_all("a", href=True):
                m = DETAIL_RE.match(urlparse(a["href"]).path)
                if m:
                    page_urls.add(urljoin(BASE, m.string))

            if not page_urls:
                break
            for u in page_urls:
                found.setdefault(u, kind)

            # Uptree serves the last page for out-of-range ?page=N rather than
            # 404ing, so "no new URLs" is the real end-of-list signal.
            if len(found) == seen_on_previous_pages:
                break
            seen_on_previous_pages = len(found)
            time.sleep(DELAY_SECONDS)

    log.info("discovered %s listings", len(found))
    return sorted(found.items())


def _extract_apply_url(soup: BeautifulSoup) -> str | None:
    """
    Placement pages carry the employer's own application link in a 'How to apply'
    line. Prefer that over sending students through Uptree's login.
    """
    anchor = soup.find(string=re.compile(r"how to apply", re.I))
    if anchor:
        # Search outward from the 'How to apply' text for the first external link.
        node = anchor.parent
        for _ in range(4):
            if node is None:
                break
            for a in node.find_all("a", href=True):
                href = a["href"]
                if href.startswith("http") and SOURCE not in href:
                    if not any(s in href for s in SOCIAL):
                        return href
            node = node.parent
    return None


def _mk_date(day: str, month: str, year: str) -> str | None:
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{day} {month} {year}", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_dates(og_title: str | None, text: str) -> tuple[str | None, str | None]:
    """
    Returns (start_date, end_date).

    Careful with ranges: "Dates: 3rd - 5th August 2026" shares one month/year
    across both days, so a naive day-month-year regex silently returns the END
    date and every listing looks like it starts on its last day.
    """
    # Events put it straight in og:title: "... - Event on  Mon, August 10, 2026"
    if og_title:
        m = EVENT_TITLE_RE.match(og_title)
        if m:
            raw = re.sub(r"\s+", " ", m.group("when")).strip()
            for fmt in ("%a, %B %d, %Y", "%A, %B %d, %Y"):
                try:
                    return datetime.strptime(raw, fmt).date().isoformat(), None
                except ValueError:
                    continue

    m = DATE_LINE_RE.search(text)
    if not m:
        return None, None
    raw = m.group("value")

    # Range sharing one month: "3rd - 5th August 2026"
    r = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s*[-\u2013\u2014]\s*"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})",
        raw,
    )
    if r:
        d1, d2, month, year = r.groups()
        return _mk_date(d1, month, year), _mk_date(d2, month, year)

    # Range with a month each side: "10th Aug ... - 21st Aug ..." (year implied)
    r = re.findall(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})", raw)
    year_m = re.search(r"(\d{4})", raw)
    if len(r) >= 2 and year_m:
        year = year_m.group(1)
        return _mk_date(*r[0], year), _mk_date(*r[-1], year)

    # Single date
    s = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})", raw)
    if s:
        return _mk_date(*s.groups()), None

    return None, None


def parse_detail(html: str, url: str, kind: str) -> RawListing | None:
    soup = BeautifulSoup(html, "html.parser")
    m = DETAIL_RE.match(urlparse(url).path)
    if not m:
        return None
    _, company_slug, numeric_id = m.groups()

    og_title = _meta(soup, "og:title")
    text = soup.get_text("\n", strip=True)

    title = og_title or ""
    if og_title:
        em = EVENT_TITLE_RE.match(og_title)
        pm = PLACEMENT_TITLE_RE.match(og_title)
        if em:
            title = em.group("title").strip()
        elif pm:
            title = pm.group("title").strip()

    # Titles are consistently "Employer: Thing, Location"
    organisation = None
    if ":" in title:
        organisation, _, remainder = title.partition(":")
        organisation = organisation.strip()
        title = remainder.strip() or title

    apply_url = _extract_apply_url(soup)
    start_date, end_date = _parse_dates(og_title, text)

    return RawListing(
        source=SOURCE,
        source_id=f"uptree:{kind}:{numeric_id}",
        kind=kind,
        url=url,
        title=title or None,
        organisation=organisation or company_slug.replace("-", " ").title(),
        description=_meta(soup, "og:description"),
        location=None,  # normalised downstream from title/body
        start_date=start_date,
        end_date=end_date,
        apply_url=apply_url or url,
        link_type="direct_apply" if apply_url else "portal_apply",
        logo_url=_meta(soup, "og:image"),
    )


def scrape() -> list[dict]:
    session = _session()
    out: list[dict] = []
    for url, kind in discover(session):
        html = _get(session, url)
        if not html:
            continue
        listing = parse_detail(html, url, kind)
        if listing and listing.title:
            out.append(asdict(listing))
        time.sleep(DELAY_SECONDS)
    log.info("scraped %s listings from %s", len(out), SOURCE)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import json

    print(json.dumps(scrape(), indent=2))
