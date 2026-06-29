"""Live ATS board discovery — the reliable finder.

Queries Greenhouse and Lever public board APIs for CURRENTLY-OPEN roles at a curated
set of Bay Area companies, filters by the candidate's target role keywords and a Bay
Area location, and returns direct application URLs. Unlike scraping a search engine
(which surfaces stale/closed postings and gets rate-limited), these APIs return live
openings as JSON in seconds — so the applier actually lands on open forms.

Greenhouse:  https://boards-api.greenhouse.io/v1/boards/<token>/jobs   -> {jobs:[{title,location:{name},absolute_url}]}
Lever:       https://api.lever.co/v0/postings/<token>?mode=json        -> [{text,categories:{location},hostedUrl}]
Ashby:       https://api.ashbyhq.com/posting-api/job-board/<org>       -> {jobs:[{title,location,jobUrl}]}

This curated set is just a FAST DEFAULT. The bot's Claude brain has WebSearch/WebFetch and is
told to scout beyond it (YC/Series-A-B/niche firms) and fetch their boards directly — so the
finder doesn't have to be exhaustive, just a reliable starting pool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.request

from bot.bay_area import is_bay_area

logger = logging.getLogger("auto-applier-discord")

# Curated companies (Bay-Area-heavy; many startups). Unknown/renamed tokens just 404 and are
# skipped, so it's safe to over-include. Tokens are the slug in boards.greenhouse.io/<token>.
GREENHOUSE = [
    # larger / established
    "stripe", "databricks", "brex", "gusto", "samsara", "airtable", "asana", "braze",
    "netlify", "coinbase", "plaid", "affirm", "sofi", "lattice", "benchling", "flexport",
    "retool", "vanta", "verkada", "cloudflare", "okta", "rubrik", "cohesity", "gitlab",
    "instacart", "doordash", "lyft", "pinterest", "reddit", "discord", "figma", "notion",
    "twilio", "anthropic", "openai", "anduril", "dropbox", "nerdwallet", "amplitude",
    "webflow", "grammarly", "deel", "scaleai", "robinhood", "chime", "faire", "gem",
    "sigmacomputing", "checkr", "samsungsemiconductor", "hashicorp", "confluent",
    # startups / Series A–C (under-the-radar, more entry-level + less competition)
    "rippling", "ramp", "vercel", "supabase", "render", "posthog", "airbyte", "census",
    "fivetran", "dbtlabs", "montecarlo", "hex", "secureframe", "drata", "snyk", "abnormal",
    "wiz", "huntress", "semgrep", "tines", "panther", "material-security", "persona",
    "alloy", "unit", "modern-treasury", "middesk", "mercury", "column", "pulley", "rutter",
    "scalapay", "tropic", "vouch", "newfront", "atbond", "found", "alma", "spring-health",
    "cityblock", "devoted-health", "clipboard-health", "incredible-health", "gather",
    "loft-orbital", "applied-intuition", "nuro", "zipline", "skydio", "shield-ai",
]
LEVER = [
    "saviynt", "hive", "thinkahead", "attentive", "ironcladhq", "plaid", "gusto",
    "huntress", "tenable", "expel", "arctic-wolf", "swimlane", "netskope", "lookout",
    "uplevel", "verkada", "matterport", "samsara", "ripple", "voleon", "kodiak",
]
# Ashby is heavily used by YC / Series-A startups — exactly the under-the-radar pool.
ASHBY = [
    "ramp", "linear", "watershed", "clay", "baseten", "modal", "together", "runway",
    "openstore", "mercury", "hex", "warp", "replit", "browserbase", "decagon", "sierra",
    "cognition", "perplexity", "harvey", "glean", "writer", "ironclad", "vanta", "pylon",
    "default", "finch", "sardine", "greenlite", "campfire", "fieldguide", "tofu",
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


def _active_keywords(query: str):
    """If the user gave a query, match on ITS terms (so different searches return
    different roles); otherwise fall back to the broad default lanes."""
    q = (query or "").strip().lower()
    if not q:
        return ROLE_KEYWORDS
    words = [w for w in re.split(r"[^a-z]+", q) if len(w) > 2]
    terms = set(words)
    terms.add(q)  # also match the whole phrase
    return list(terms) or ROLE_KEYWORDS


def _matches_role(title: str, keywords) -> bool:
    t = title.lower()
    if any(k in t for k in keywords):
        if any(b in t for b in _EXCLUDE):
            return False
        return True
    return False


def _greenhouse(token: str, keywords) -> list[str]:
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
        if _matches_role(j.get("title", ""), keywords) and is_bay_area(loc, j.get("title", "")):
            url = j.get("absolute_url")
            if url:
                out.append(url)
    return out


def _lever(token: str, keywords) -> list[str]:
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
        if _matches_role(j.get("text", ""), keywords) and is_bay_area(loc, j.get("text", "")):
            url = j.get("hostedUrl") or j.get("applyUrl")
            if url:
                out.append(url)
    return out


def _ashby(org: str, keywords) -> list[str]:
    try:
        req = urllib.request.Request(
            f"https://api.ashbyhq.com/posting-api/job-board/{org}",
            headers={"User-Agent": "applier/1.0"})
        jobs = json.load(urllib.request.urlopen(req, timeout=15)).get("jobs", [])
    except Exception as exc:  # noqa: BLE001
        logger.info("job_boards: ashby/%s skipped (%s)", org, exc.__class__.__name__)
        return []
    out = []
    for j in jobs:
        loc = j.get("location") or ""
        title = j.get("title", "")
        if _matches_role(title, keywords) and is_bay_area(str(loc), title):
            url = j.get("jobUrl") or j.get("applyUrl") or j.get("jobPostingUrl")
            if url:
                out.append(url)
    return out


async def find_board_jobs(max_results: int = 25, query: str = "") -> list[str]:
    """Concurrently query all curated Greenhouse/Lever/Ashby boards; return up to
    max_results open Bay-Area application URLs. Query-aware (matches the user's terms
    when given) and shuffled across companies so repeated searches don't resurface the
    same pool. De-duplicated, interleaved so one big board doesn't crowd out the rest."""
    kw = _active_keywords(query)
    gh = await asyncio.gather(*[asyncio.to_thread(_greenhouse, t, kw) for t in GREENHOUSE])
    lv = await asyncio.gather(*[asyncio.to_thread(_lever, t, kw) for t in LEVER])
    ash = await asyncio.gather(*[asyncio.to_thread(_ashby, t, kw) for t in ASHBY])
    per_company = [c for c in (gh + lv + ash) if c]
    random.shuffle(per_company)  # vary which companies lead between calls
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
    logger.info("job_boards: %d open Bay-Area matches across %d boards (query=%r)",
                len(out), len(per_company), query or "<default lanes>")
    return out


def _slugify(name: str) -> list[str]:
    """Plausible ATS slug forms for a company name the bot discovered."""
    base = name.strip().lower()
    if base.startswith(("http://", "https://")):
        # already a board URL — let the caller WebFetch it; nothing to slugify
        return []
    compact = re.sub(r"[^a-z0-9]", "", base)            # "Modern Treasury" -> "moderntreasury"
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")  # -> "modern-treasury"
    return list(dict.fromkeys(t for t in (compact, hyphen) if t))


def probe_company(name: str, query: str = "") -> list[str]:
    """Pull a company's CURRENT open Bay-Area role URLs from whichever of its
    Greenhouse / Lever / Ashby boards resolves — by slug. This is the bridge from
    the bot's open-ended discovery (WebSearch finds a company name) to reliable,
    structured live data, for ANY company, not just the curated seed."""
    kw = _active_keywords(query)
    out: list[str] = []
    seen: set[str] = set()
    for slug in _slugify(name):
        for fetch in (_greenhouse, _lever, _ashby):
            for u in fetch(slug, kw):
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        if out:
            break  # first slug that resolves to roles wins
    return out
