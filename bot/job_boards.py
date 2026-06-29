"""Live ATS board discovery — the reliable finder.

Queries Greenhouse and Lever public board APIs for CURRENTLY-OPEN roles at a curated
set of Bay Area companies, filters by the candidate's target role keywords and a Bay
Area location, and returns direct application URLs. Unlike scraping a search engine
(which surfaces stale/closed postings and gets rate-limited), these APIs return live
openings as JSON in seconds — so the applier actually lands on open forms.

Greenhouse:  https://boards-api.greenhouse.io/v1/boards/<token>/jobs   -> {jobs:[{title,location:{name},absolute_url}]}
Lever:       https://api.lever.co/v0/postings/<token>?mode=json        -> [{text,categories:{location},hostedUrl}]
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from bot.bay_area import is_bay_area

logger = logging.getLogger("auto-applier-discord")

# Curated Bay-Area companies. Unknown/renamed tokens just 404 and are skipped, so it's
# safe to over-include. Tokens are the slug in boards.greenhouse.io/<token>.
GREENHOUSE = [
    "stripe", "databricks", "brex", "gusto", "samsara", "airtable", "asana", "braze",
    "netlify", "coinbase", "plaid", "affirm", "sofi", "lattice", "benchling", "flexport",
    "retool", "vanta", "verkada", "cloudflare", "okta", "rubrik", "cohesity", "gitlab",
    "instacart", "doordash", "lyft", "pinterest", "reddit", "discord", "figma", "notion",
    "twilio", "anthropic", "openai", "anduril", "dropbox", "nerdwallet", "amplitude",
    "webflow", "grammarly", "deel", "scaleai", "robinhood", "chime", "faire", "gem",
    "sigmacomputing", "checkr", "samsungsemiconductor", "hashicorp", "confluent",
]
LEVER = [
    "saviynt", "hive", "thinkahead", "attentive", "ironcladhq", "plaid", "gusto",
]

# Role keywords. The goal is to land the candidate ANY entry-level Bay-Area job —
# his background is cybersecurity, but he's open to sales/BDR, ops, support, and
# general white-collar work. So this spans cyber + the common entry-level lanes a
# recent grad gets hired into. The senior-title exclusion below keeps it junior.
ROLE_KEYWORDS = [
    # cybersecurity / IT (his background)
    "security", "soc", "cyber", "grc", "risk", "analyst", "information security",
    "infosec", "threat", "incident", "compliance", "it support", "help desk",
    "service desk", "desktop support", "technical support",
    # sales / business development
    "sales development", "business development", "bdr", "sdr", "sales representative",
    "account representative", "inside sales",
    # customer / client facing
    "customer success", "customer support", "customer experience", "client services",
    "support specialist", "onboarding", "implementation",
    # operations / general entry-level white-collar
    "operations associate", "operations coordinator", "business operations",
    "associate", "coordinator", "specialist", "data analyst", "business analyst",
    "recruiting coordinator", "people operations", "administrative",
]

# Titles to skip (substring match): too senior for an early-career candidate, or
# specialist software-engineering roles he isn't a fit for (Jack: "he won't get a
# SWE job"). Security *analyst* roles still pass; security *engineer* coding roles
# are dropped along with general SWE.
_EXCLUDE = (
    # seniority
    "senior", "staff", "principal", "director", "vp ", "vice president", "head of",
    "manager", "lead ", "architect", "executive", " ii", " iii", " iv",
    # software-engineering ICs he can't fill
    "software engineer", "software developer", "developer", "data scientist",
    "machine learning", "devops", "backend", "front end", "frontend", "full stack",
    "full-stack", "mobile engineer", "platform engineer", "firmware",
)


def _matches_role(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in ROLE_KEYWORDS):
        if any(b in t for b in _EXCLUDE):
            return False
        return True
    return False


def _greenhouse(token: str) -> list[str]:
    try:
        req = urllib.request.Request(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            headers={"User-Agent": "applier/1.0"})
        jobs = json.load(urllib.request.urlopen(req, timeout=15)).get("jobs", [])
    except Exception as exc:  # noqa: BLE001 — 404/timeouts are expected, just skip
        logger.info("job_boards: greenhouse/%s skipped (%s)", token, exc.__class__.__name__)
        return []
    out = []
    for j in jobs:
        loc = (j.get("location") or {}).get("name", "")
        if _matches_role(j.get("title", "")) and is_bay_area(loc, j.get("title", "")):
            url = j.get("absolute_url")
            if url:
                out.append(url)
    return out


def _lever(token: str) -> list[str]:
    try:
        req = urllib.request.Request(
            f"https://api.lever.co/v0/postings/{token}?mode=json",
            headers={"User-Agent": "applier/1.0"})
        jobs = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception as exc:  # noqa: BLE001
        logger.info("job_boards: lever/%s skipped (%s)", token, exc.__class__.__name__)
        return []
    out = []
    for j in jobs:
        loc = (j.get("categories") or {}).get("location", "")
        if _matches_role(j.get("text", "")) and is_bay_area(loc, j.get("text", "")):
            url = j.get("hostedUrl") or j.get("applyUrl")
            if url:
                out.append(url)
    return out


async def find_board_jobs(max_results: int = 25) -> list[str]:
    """Concurrently query all curated boards; return up to max_results open Bay-Area
    application URLs matching the candidate's roles. De-duplicated, interleaved across
    companies so one big board doesn't crowd out the rest."""
    gh = await asyncio.gather(*[asyncio.to_thread(_greenhouse, t) for t in GREENHOUSE])
    lv = await asyncio.gather(*[asyncio.to_thread(_lever, t) for t in LEVER])
    per_company = gh + lv
    # round-robin interleave so the batch spans many companies
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while len(out) < max_results and any(i < len(c) for c in per_company):
        for c in per_company:
            if i < len(c):
                u = c[i]
                if u not in seen:
                    seen.add(u)
                    out.append(u)
                    if len(out) >= max_results:
                        break
        i += 1
    logger.info("job_boards: %d open Bay-Area role matches across %d boards",
                len(out), sum(1 for c in per_company if c))
    return out
