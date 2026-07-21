"""
Relevance gate — decides what belongs on Bridge.

This encodes the rulebook the user wrote (see RULES.md). It is deliberately
DETERMINISTIC (plain rules, no AI) so Session 1 needs no API key and its
decisions are testable and predictable. The AI extractor in Session 2 will add
judgement for the genuinely ambiguous cases; this handles the clear ones.

Design choices that matter:
  - Nothing is ever deleted. Off-topic rows get is_live=False and sit in the
    table. Flip one back by hand if you disagree.
  - Hard "off-topic" title words override everything - a mill operative role at
    a bank is still a mill operative role.
  - The employer is a strong positive signal (a Deloitte page with an empty
    description is still finance).
  - When a row is genuinely borderline, it is NOT silently binned - it is
    marked for review so a human decides.
"""

from __future__ import annotations

# The 8 subjects the whole site is about.
CORE_SUBJECTS = {
    "business", "economics", "finance", "politics",
    "philosophy", "psychology", "law", "media",
}

# Hard NO. Whatever the employer, whatever the tagger guessed - these are
# real jobs on the wrong site (medicine, trades, technical, manual).
OFF_TOPIC_TITLE_WORDS = [
    "mill operative", "lab technician", "laboratory", "maintenance technician",
    "engineer", "engineering", "electrician", "welder", "fitter", "machinist",
    "machine operator", "warehouse", "forklift", "hgv", "lorry", "driver",
    "nurse", "nursing", "midwife", "paramedic", "dental", "dentist",
    "chef", "kitchen", "catering", "cleaner", "labourer", "plumber",
    "carpenter", "bricklayer", "scaffolder", "nutrition", "dietitian",
    "veterinary", "pharmacy", "pharmacist", "beautician", "hairdresser",
    "mechanic", "plasterer", "roofer", "groundworker",
]

# Junk patterns - not a real opportunity regardless of category.
JUNK_TITLE_WORDS = [
    "win an ipad", "win a", "prize draw", "giveaway", "raffle",
    "register your interest for 2027", "register your interest for upcoming",
    "share our petition", "sign our petition",
]

# Employer -> subjects. The strongest signal we have, because some listing
# pages ship an empty description and the title alone ("ASPIRE Work Experience")
# names no subject at all.
EMPLOYER_SUBJECTS = {
    "deloitte": ["business", "finance", "economics"],
    "kpmg": ["business", "finance", "economics"],
    "pwc": ["business", "finance", "economics"],
    "pricewaterhouse": ["business", "finance", "economics"],
    "ey": ["business", "finance", "economics"],
    "ernst": ["business", "finance", "economics"],
    "grant thornton": ["business", "finance"],
    "bdo": ["business", "finance"],
    "mazars": ["business", "finance"],
    "jpmorganchase": ["finance", "business", "economics"],
    "jp morgan": ["finance", "business", "economics"],
    "goldman": ["finance", "business", "economics"],
    "morgan stanley": ["finance", "business", "economics"],
    "barclays": ["finance", "business", "economics"],
    "lloyds banking group": ["finance", "business", "economics"],
    "hsbc": ["finance", "business"],
    "natwest": ["finance", "business"],
    "santander": ["finance", "business"],
    "nomura": ["finance", "economics"],
    "citi": ["finance", "economics"],
    "bank of america": ["finance", "economics"],
    "schroders": ["finance", "economics"],
    "blackrock": ["finance", "economics"],
    "fidelity": ["finance", "economics"],
    "crisil": ["finance", "economics"],
    "coalition greenwich": ["finance", "economics"],
    "bank of england": ["economics", "finance", "politics"],
    "addleshaw goddard": ["law", "business"],
    "taylor wessing": ["law", "business"],
    "winston taylor": ["law", "business"],
    "slaughter and may": ["law", "business"],
    "linklaters": ["law", "business"],
    "clifford chance": ["law", "business"],
    "allen & overy": ["law", "business"],
    "a&o shearman": ["law", "business"],
    "freshfields": ["law", "business"],
    "herbert smith": ["law", "business"],
    "hogan lovells": ["law", "business"],
    "norton rose": ["law", "business"],
    "civil service": ["politics", "economics"],
    "gchq": ["politics"],
    "intelligence agencies": ["politics"],
    "mi5": ["politics"],
    "mi6": ["politics"],
    "home office": ["politics"],
    "parliament": ["politics"],
    "bbc": ["media", "politics"],
    "sky": ["media"],
    "itv": ["media"],
    "ogilvy": ["media", "business"],
    "wpp": ["media", "business"],
}

# Top-tier US universities whose pre-college programmes are allowed.
US_TOP_UNIS = [
    "harvard", "yale", "princeton", "columbia", "wharton", "upenn",
    "university of pennsylvania", "brown", "cornell", "dartmouth",
    "stanford", "mit", "massachusetts institute", "uchicago",
    "university of chicago", "berkeley", "duke", "northwestern",
]

# Countries/regions allowed for summer schools. Everything else is out.
SUMMER_SCHOOL_ALLOWED_HINTS = [
    "uk", "united kingdom", "england", "scotland", "wales", "london",
    "oxford", "cambridge", "europe", "belgium", "france", "germany",
    "netherlands", "spain", "italy", "ireland", "switzerland", "online",
    "virtual", "remote",
] + US_TOP_UNIS

# Volunteering is the only local section: London + Kent only.
VOLUNTEERING_ALLOWED = [
    "london", "kent", "sevenoaks", "bromley", "greenwich", "lewisham",
    "croydon", "bexley", "dartford", "gravesend", "maidstone", "tunbridge",
    "tonbridge", "sevenoaks", "canterbury", "rochester", "chatham",
    "online", "remote", "virtual",
]


def _has(text: str, words) -> bool:
    low = text.lower()
    return any(w in low for w in words)


def subjects_for(title: str, description: str, organisation: str) -> list[str]:
    """Best-guess subjects: keyword hits in the text, plus the employer map."""
    hay = f"{title} {description} {organisation}".lower()
    hits = set()
    keyword_map = {
        "finance": ["finance", "banking", "investment", "audit", "tax",
                    "accountancy", "accounting", "insurance", "actuarial"],
        "business": ["business", "consulting", "professional services",
                     "marketing", "commerce", "management", "entrepreneur",
                     "enterprise", "startup", "pitch"],
        "economics": ["economics", "economic", "markets", "trade"],
        "law": ["law", "legal", "solicitor", "barrister", "compliance"],
        "politics": ["politics", "policy", "parliament", "government",
                     "civil service", "diplomacy", "international relations"],
        "psychology": ["psychology", "psychological", "mental health", "wellbeing"],
        "philosophy": ["philosophy", "ethics", "philosophical"],
        "media": ["media", "journalism", "communications", "broadcast",
                  "publishing", "advertising"],
    }
    for subj, words in keyword_map.items():
        if any(w in hay for w in words):
            hits.add(subj)

    org_low = (organisation or "").lower()
    for name, subs in EMPLOYER_SUBJECTS.items():
        if name in org_low:
            hits.update(subs)
            break

    return sorted(hits)


def assess(row: dict) -> dict:
    """
    Returns {'decision': 'show'|'hide'|'review', 'reason': str, 'subjects': [...]}.
    Never raises, never deletes - callers translate 'show' -> is_live True.
    """
    title = (row.get("title") or "")
    description = (row.get("description") or "")
    organisation = (row.get("organisation") or "")
    location = (row.get("location") or "")
    category = (row.get("category") or "").lower()

    # 1. Junk - out, no argument.
    if _has(title, JUNK_TITLE_WORDS):
        return {"decision": "hide", "reason": "junk/promo pattern", "subjects": []}

    # 2. Off-topic trade/technical/medical - out, whatever the employer.
    if _has(title, OFF_TOPIC_TITLE_WORDS):
        return {"decision": "hide", "reason": "off-topic (wrong field)", "subjects": []}

    subjects = subjects_for(title, description, organisation)

    # 3. Category-specific geography rules.
    if "summer" in category or "summer_school" in category:
        # Allowed only if some allowed region/uni hint appears somewhere.
        blob = f"{title} {description} {location} {organisation}"
        if not _has(blob, SUMMER_SCHOOL_ALLOWED_HINTS):
            return {"decision": "review",
                    "reason": "summer school with no clear UK/Europe/top-US signal",
                    "subjects": subjects}

    if "volunteer" in category:
        blob = f"{title} {description} {location}"
        if not _has(blob, VOLUNTEERING_ALLOWED):
            return {"decision": "hide",
                    "reason": "volunteering outside London/Kent",
                    "subjects": subjects}
        # In-area volunteering that passed the junk + off-topic gates is fine.
        # We don't demand a subject keyword - a Kent food bank is self-evidently
        # a meaningful volunteering role, not a business listing.
        return {"decision": "show", "reason": "volunteering in area", "subjects": subjects}

    # Competitions and summer schools that cleared the gates above are relevant
    # by virtue of their category - an essay prize or an Oxford summer school
    # doesn't need a subject KEYWORD in its title to belong here.
    if "competition" in category or "summer" in category:
        return {"decision": "show", "reason": f"relevant ({category})", "subjects": subjects}

    # 4. For the remaining categories (internships, work experience, insight,
    #    online courses) require a real subject connection. No signal at all is
    #    more likely a thin page than off-topic, so review rather than bin.
    if not (set(subjects) & CORE_SUBJECTS):
        return {"decision": "review",
                "reason": "no clear subject match",
                "subjects": subjects}

    return {"decision": "show", "reason": "relevant", "subjects": subjects}
