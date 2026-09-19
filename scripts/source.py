#!/usr/bin/env python3
"""source.py — rotating job sourcer. Run this at the START of every apply wave.

Why it exists: web search returns the same (often stale) postings every session, and
the brain re-mines its favourite five companies, so waves run dry on jobs already in
applied.csv/seen.csv. This queries live ATS board APIs across a large pool of boards
and *rotates* which ones it hits, so consecutive sessions see different companies.

    python3 scripts/source.py                     # 30 fresh jobs from 400 rotated boards
    python3 scripts/source.py --n 60 --boards 900 # deeper backlog for a long wave
    python3 scripts/source.py --lane security    # only one lane's keywords
    python3 scripts/source.py --stats            # rotation state, no fetching

Output is TSV — url, company, title, location, lane — one job per line, already
deduped against applied.csv + seen.csv and already location/seniority filtered. Fit
decisions and the apply itself are still yours; this only decides what to look at.

Rotation: data/board_rotation.csv holds last_mined/mines/hits/fails per board. Boards
are sampled weighted by staleness × productivity (Efraimidis–Spirakis), so the
longest-unmined boards are the likeliest picks, boards that have produced a matching
job get a boost, and boards mined 3+ times without one get demoted (never dropped —
postings turn over). Every board keeps a real chance: that randomness is what stops
each session converging on the same pool. Boards that 404 four times running are
pruned automatically — but only real 404s count. Greenhouse answers 406 when it is
throttling, and a throttled board is left completely untouched so a bad minute on
their side can't quietly delete good boards from the pool.

Default platforms are Greenhouse + Lever only — Ashby is Tier 3 HARD-AVOID per
CLAUDE.md SITE ROUTING (blocks at submit regardless of IP), so sourcing it would fill
a wave with applications that can never land. Pass --platforms to override.

Pool lives in scripts/companies.txt (`platform:token`) — ~2,100 verified-live boards.
Add companies there; never hardcode them here.

RUN scripts/search_jobs.py FIRST (Jack, 2026-09-18). Now that the lanes are cyber + IT
support only, a pool sample is the wrong primary source: most boards have no cyber
opening on any given day, so a pool-first wave reports the market exhausted while real
postings sit one search away. search_jobs.py starts from the TITLE, finds the employer,
probes that board and appends it here — so this pool is a cache of what search has
already turned up, and this script is the TOP-UP for a wave search left short.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import random
import re
import subprocess
import sys
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
from url_norm import normalize as _norm  # noqa: E402

# Bay Area terms, inlined from bot/bay_area.py — the deployed VPS runtime has no bot/
# directory, so this script must stand alone. Keep the two lists in sync if either moves.
# Names that are ONLY Bay Area places get a bare entry. Names that also exist elsewhere
# in the world are qualified with ", ca" — "Brisbane" alone matched an Australian BDR
# role on 2026-07-19, and Albany/Lafayette/Saratoga/Union City/Richmond/Danville/
# Concord/Belmont all have better-known namesakes outside California.
BAY_TERMS = (
    "bay area", "sf bay", "san francisco bay", "silicon valley", "the peninsula",
    "north bay", "east bay", "south bay",
    "san francisco", "sf,", "s.f.", "daly city", "brisbane, ca", "south san francisco",
    "san mateo", "redwood city", "palo alto", "menlo park", "foster city",
    "los altos", "colma",
    "burlingame", "san bruno", "millbrae", "san carlos", "belmont, ca", "half moon bay",
    "east palo alto",
    "san jose", "santa clara", "sunnyvale", "mountain view", "cupertino", "milpitas",
    "los gatos", "campbell, ca", "saratoga, ca", "morgan hill", "gilroy",
    "oakland", "berkeley", "emeryville", "alameda", "fremont, ca", "hayward, ca",
    "richmond, ca", "walnut creek", "pleasanton", "dublin, ca", "san ramon",
    "concord, ca", "union city, ca", "newark, ca", "castro valley", "san leandro",
    "pleasant hill", "danville, ca", "livermore", "martinez, ca", "lafayette, ca",
    "orinda", "albany, ca",
    "marin county", "sausalito", "san rafael", "novato", "mill valley", "corte madera",
    "larkspur", "tiburon", "petaluma",
    # Outer ring of the nine-county Bay Area (Jack, 2026-07-20). Names unique to CA go
    # in bare; ones with well-known namesakes are qualified — "pittsburg" is a prefix of
    # "Pittsburgh, PA", Fairfield exists in CT/NJ/OH/IA, Antioch in TN/IL, Brentwood in
    # TN/NY and in Los Angeles, Hillsborough in NC/NJ.
    "santa rosa", "napa", "vallejo", "benicia", "pacifica", "el cerrito", "pinole",
    "atherton", "fairfield, ca", "antioch, ca", "brentwood, ca", "pittsburg, ca",
    "hillsborough, ca",
)


def is_bay_area(*texts: str) -> bool:
    """True iff any text names a Bay Area location. Strict: unknown/empty → False."""
    blob = " ".join(t for t in texts if t).lower()
    return bool(blob.strip()) and any(term in blob for term in BAY_TERMS)

# Every outbound request identifies as a normal desktop browser. The old "applier/1.0"
# announced a bot to every WAF it touched, and Greenhouse blocked the whole domain from
# this box for hours on 2026-08-08. These are public, unauthenticated board APIs — the
# point is not to be sneaky, it is to stop looking like the thing rate-limiters exist to
# stop. Throttling (see verify_rows) does the actual work of being a good citizen.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")

POOL_PATH = ROOT / "scripts" / "companies.txt"
ROTATION_PATH = ROOT / "data" / "board_rotation.csv"
APPLIED_PATH = ROOT / "data" / "applied.csv"
SEEN_PATH = ROOT / "data" / "seen.csv"
RETRY_PATH = ROOT / "data" / "retry.csv"  # already queued for another attempt — don't re-surface
BLOCKLIST_PATH = ROOT / "data" / "blocklist.txt"  # "<ats_host>\t<company>\t<reason>"
# Tier 1 per CLAUDE.md SITE ROUTING — the only platforms that actually convert. Ashby
# blocks at submit regardless of IP (Tier 3 HARD-AVOID), so it is OFF by default: sourcing
# Ashby jobs just fills a wave with applications that can never land.
# Every platform prefix companies.txt may use. Keep load_pool() and the --platforms
# validator reading from THIS -- they used to hardcode their own copies, which is how
# breezy boards ended up parsed out of the pool while being sourced by default.
KNOWN_PLATFORMS = ("greenhouse", "lever", "breezy", "workable", "ashby",
                   "recruitee", "teamtailor")
DEFAULT_PLATFORMS = ("greenhouse", "lever", "breezy", "recruitee", "teamtailor")
# How often a job SURFACED on each platform becomes a job APPLIED TO, measured 2026-08-03
# over all 3,750 seen / 732 applied. Greenhouse 23.7% (counting the vanity careers domains
# its absolute_url returns -- brex.com, navan.com and 60 other hosts are Greenhouse boards
# wearing a company skin), breezy 15.0%, lever 7.9%, ashby 5.5%, workable 0 of 123.
#
# pick_boards already had a productivity term, but it counts JOBS SURFACED, not applications
# submitted -- so a Lever board crowded with postings outranks a Greenhouse board even when
# every one of those postings dies at an hCaptcha. This is the missing half of that signal.
# Nothing is zeroed: the low end is a floor, not a ban, so a platform can climb back out if
# its wall ever comes down. Breezy's 0.65 rests on only 60 surfaced jobs -- a prior, not a
# measurement; it will firm up now that discovery has put 1,044 breezy boards in the pool.
PLATFORM_CONVERSION = {
    "greenhouse": 1.00,
    "breezy": 0.65,
    # Added 2026-08-04 with no conversion data of their own yet, so these are PRIORS, not
    # measurements — set mid-pack so the sampler gives them enough traffic to earn a real
    # number without letting them crowd out Greenhouse before they have proved anything.
    # Revisit once apply_events.jsonl has a few dozen attempts on each. Their forms are
    # unproven: the board APIs are clean, but whether the APPLY page is captcha-walled the
    # way Ashby and Workable are is exactly what these first waves will find out.
    "recruitee": 0.50,
    "teamtailor": 0.50,
    "lever": 0.35,
    "ashby": 0.25,
    "workable": 0.10,
}
ROTATION_HEADER = ["platform", "token", "last_mined", "mines", "hits", "fails", "posts"]
# A board that returns this many postings PER MINE, and never once surfaced a job in
# ANY lane, is not an employer in his market. Measured 2026-08-09 over 796 randomly
# sampled boards: median board returns 8-10 postings per mine and p90 is 65, so 200 is
# deep in the tail — it catches exactly the franchise and staffing megaboards (svetness
# 4,979/mine, plscareers 776/mine) that were 41.7% of all fetch volume and 0% of all
# supply.
#
# 2026-09-04: this was previously compared against the CUMULATIVE `posts` counter, which
# `mine()` increments every single mine. The 400 threshold was calibrated on per-mine
# size but applied to a lifetime total, so an ordinary 9-postings-per-mine board crossed
# it after ~44 mines and was pruned forever. It ran backwards — the more active an
# employer, the faster it died (9/mine died in 44 mines; a near-dead 1/mine board
# survived 324). It was deleting 75% of the pool (6,412 of 8,511 boards) and is the
# cause of the "800 boards -> 31,687 postings -> 0 servable" collapse.
BARREN_PER_MINE = 200
DEAD_AFTER = 4  # consecutive fetch failures before a board is dropped from rotation


def _posts_per_mine(s: dict) -> float:
    """Average postings returned per mine. `posts` is cumulative, `mines` is the count."""
    return s.get("posts", 0) / max(s.get("mines", 0), 1)

# --- lanes -------------------------------------------------------------------
# Ordered by conversion odds for a zero-experience candidate (see CLAUDE.md
# "Sourcing priority"). Tier 1 lanes are ranked first in the output; within a tier
# the order is random so no single lane monopolises the top of every wave.
LANES: dict[str, tuple[int, tuple[str, ...]]] = {
    "security": (1, (
        "security analyst", "soc analyst", "soc ", "cyber", "grc", "governance risk",
        # The helpdesk-to-SOC titles spelled as a tier. "tier 3" stays killed in
        # EXCLUDE; tier 1/2 is the rung he is applying for.
        "tier 1 analyst", "tier 2 analyst", "tier i analyst", "tier ii analyst",
        "information security", "infosec", "security operations", "threat",
        "incident response", "vulnerability", "compliance analyst", "risk analyst",
        "security compliance", "trust and safety", "trust & safety",
        # Security-consultancy titles. The candidate applied to three Coalfire FedRAMP roles on
        # 2026-07-19 that source.py couldn't see — it found them only by browsing the
        # board directly. These are squarely in-lane for a CompTIA-certified candidate.
        "fedramp", "soc 2", "iso 27001", "security assessment", "security consultant",
        "compliance consultant", "identity and access", "iam analyst",
        # Trust & safety / abuse enforcement uses its own vocabulary. Anthropic alone
        # had 13 invisible postings ("Safeguards Enforcement Analyst, Ban Evasion",
        # "Safeguards Policy Analyst, Fraud & Scams"). "governance, risk" with the
        # comma is how Everlaw writes a literal GRC Analyst req.
        "safeguards", "enforcement analyst", "content moderat", "abuse",
        "governance, risk", "grc analyst",
        # Investigations — his degree is criminology, and these titles matched nothing.
        "investigator", "investigations", "loss prevention", "background screen",
        "due diligence", "sanctions", "anti-money laundering", "financial crime",
        "case investigator", "claims investigator", "physical security",
        # Federal / compliance / audit vocabulary (Jack, 2026-09-18). None of this
        # matched before, which is most of why only 112 of 707 tailored applications
        # went out in this lane: an "IT Audit Analyst (SOX)", an "ISSO", or a "Security
        # Controls Assessor" req was simply invisible to the sourcer.
        "rmf", "risk management framework", "authorization to operate", "emass",
        "nist 800-53", "800-171", "fisma", "cmmc", "isso", "issm",
        "security controls assessor", "controls assessor",
        "systems security officer", "information system security",
        "assessment and authorization", "sox", "itgc", "it audit",
        "internal controls", "audit readiness", "soc 1",
        "third party risk", "third-party risk", "tprm", "vendor risk",
        "privacy analyst", "insider threat", "siem", "splunk",
        "cyber defense", "security assurance", "security governance", "cloud security",
    )),
    # The ONLY feeder lane left (Jack, 2026-09-18). Help desk -> SOC is a real path;
    # sdr/people/finance/cs/ops/analyst/content were deleted outright. They were not
    # ranked below security, they were competing with it: 370 sdr + 225 ops applications
    # against 112 security, because the cyber supply is thin in any single wave and the
    # other lanes always had something to fill the queue with.
    "it": (2, (
        "it support", "help desk", "helpdesk", "service desk", "desktop support",
        "technical support", "it specialist", "it associate", "systems support",
    )),
}


def _tier(lane: str) -> int:
    """Tier for a lane, tolerating a lane that is no longer a target.

    Every lane in the pipeline comes from classify(), so it is always a current one — but
    seen.csv/retry.csv are full of `sdr`/`ops` rows from the 1,227 applications sent
    before the cyber-only narrowing, and a bare LANES[lane] anywhere those strings reach
    would raise KeyError and kill the whole run rather than drop one row.
    """
    return LANES.get(lane, (99, ()))[0]

# Substring kill-list — seniority he can't reach, and technical ICs needing a coding
# interview. Mirrors the HARD FIT FILTER in CLAUDE.md; keep the two in sync.
#
# Calibrated 2026-07-19 against 50,373 live postings. Terms are only as broad as the
# evidence supports — an over-broad term here is invisible, because the job simply
# never appears and nothing records that it was dropped:
#   - bare "executive" killed "Executive IT Support Specialist" (SpaceX, Palo Alto),
#     "Account Executive - New Grad", and "Assistant Account Executive" (the entry rank
#     in PR). Narrowed to the specific quota titles.
#   - the contract/temp family is GONE. profile.yaml's own excluded_title_keywords are
#     only senior/staff/principal/lead/manager/director, and the candidate's own resume lists
#     "Cybersecurity Content Consultant (Subcontractor)" — he has done contract work.
#     It was killing "Recruiting Coordinator (Contract)" at Vercel and Mainspring,
#     which are among the highest-conversion openings a zero-experience candidate gets.
#   - " ii" killed "Associate Operations Support Specialist II" and "IT Support
#     Specialist II". Level II is 1-2 years, not out of reach. III/IV stay.
EXCLUDE = (
    "senior", "sr.", "sr ", "(sr", "sr)", "staff", "principal", "director", "vp ",
    "vice president", "head of", "manager", "lead ", " lead", "architect", "chief",
    "account executive", "executive director", "executive assistant", "sales executive",
    "client executive", "business partner", "commander", "sourcer",
    "part time", "part-time", "seasonal", "driver", "success executive",
    # On-call and mailroom clerical reqs. Same class as the part-time/seasonal/data-entry
    # drops above: on-site by definition, so unreachable from Chantilly, and not a step
    # toward the security/SDR work he is certified for. "contract" and "temporary" stay
    # ALLOWED on purpose -- see the Vercel/Mainspring and Info-Security-Analyst-Temp
    # notes in this module's docstring history; duration is not the objection.
    "on call", "on-call", "mailroom", "mail room",
    " iii", " iv", "level 3",
    # " ii" joins them: every "Engineer II"/"Analyst II" this pipeline has opened stated
    # 4-5 years. "Tier 1/2 analyst" is a different thing and stays in-lane.
    " ii ", " ii)", "(ii)", "level 2 engineer",
    "software engineer", "software developer", "developer", "engineer", "engineering",
    "data scientist", "machine learning", "devops", "backend", "back end", "frontend",
    "front end", "full stack", "full-stack", "firmware", "scientist", "researcher",
    "counsel", "attorney", "paralegal", "legal", "nurse", "clinical", "physician",
    "pharmac", "cpa", "tax ", "accountant", "phd",
    # Bare "audit" used to live on the line above and was killing the whole SOX/ITGC/
    # IT-audit family, which is squarely in-lane for a GRC candidate (Jack, 2026-09-18).
    # Narrowed to the accounting-side titles; "cpa"/"tax "/"accountant" still stand.
    "financial statement audit", "external audit", "audit senior",
    # Pipelines and evergreen reqs, not open roles — applying to these goes nowhere.
    "work at home", "work from home", "data entry",
    "expression of interest", "talent pool", "talent community", "general application",
    "future opportunity", "join our",
    # Geographic restriction spelled out in the title rather than the location, e.g.
    # "Sales Representative (Central Midwest Candidates Only)" filed under "United States".
    "candidates only", "residents only", "must reside in", "must be located in",
    # Clearance (Jack, 2026-09-18): "cleared" and "clearance" used to be killed here on
    # the grounds that the candidate held none. He is now in final adjudication for a
    # SECRET, so cleared/clearance postings are back in — they are a large share of the
    # cyber market. What a Secret does NOT reach still stands:
    "ts/sci", "polygraph", "full scope", "ci poly", "top secret",
    "tier 3",  # escalation tier — senior by definition; tier 1/2 analyst stays in-lane
    # "compliance analyst" is in-lane vocabulary, but it also matches customs/export
    # work that has nothing to do with security compliance — the first live cyber wave
    # queued "Trade Compliance Analyst" at Magnet Forensics off that keyword.
    "trade compliance", "export compliance", "export control", "customs",
)
# Support/IT/SecOps titles that legitimately carry the word "engineer". These are
# exempt from the engineer/developer family below — see the carve-out in classify().
SUPPORT_SAFE = (
    "technical support", "support engineer", "it support", "help desk", "helpdesk",
    "service desk", "desktop support", "customer support", "customer success",
    "implementation engineer", "security operations", "content developer",
    # Security titles that carry "engineer" without carrying a coding interview
    # (Jack, 2026-09-18). CODING_HEAVY below still applies to every one of them.
    "security engineer", "cloud security", "information security engineer",
    "compliance engineer", "iam engineer", "identity engineer",
)
# Applied to EVERY title, including the SUPPORT_SAFE carve-outs — the carve-out exists
# to let "Cloud Security Engineer I" through, not to readmit a coding interview. The
# forward-deployed/solutions/sales-engineer ban lived only in CLAUDE.md prose until now,
# which is how a wave applied to an xAI Forward Deployed Engineer.
CODING_HEAVY = (
    "application security", "appsec", "product security", "detection engineer",
    "detection and response engineer", "security software", "reverse engineer",
    "malware", "offensive security", "exploit", "penetration test", "red team",
    "forward deployed", "solutions engineer", "sales engineer",
)
ENGINEER_FAMILY = ("software engineer", "software developer", "developer", "engineer",
                   "engineering")
_EXCLUDE_NO_ENG = tuple(x for x in EXCLUDE if x not in ENGINEER_FAMILY)
# Internships only — the bare substring "intern" also matched "Internal Communications
# Specialist" (Penumbra, Alameda) and anything "International".
_INTERNSHIP = re.compile(r"\bintern(ship)?s?\b")

# Signals a posting is calibrated for zero experience — these get ranked up.
EARLY_SIGNALS = (
    "new grad", "early career", "entry level", "entry-level", "associate", "trainee",
    "rotational", "apprentice", " i ", "university", "graduate",
)
# Non-US markers that disqualify an otherwise-"remote" posting.
NON_US = (
    "canada", "toronto", "vancouver", "montreal", "uk", "united kingdom", "london",
    "ireland", "emea", "europe", "germany", "berlin", "munich", "france",
    "paris", "spain", "madrid", "barcelona", "portugal", "lisbon", "netherlands",
    "amsterdam", "poland", "warsaw", "krakow", "romania", "india", "bangalore",
    "hyderabad", "pune", "singapore", "japan", "tokyo", "australia", "sydney",
    "melbourne", "israel", "tel aviv", "brazil", "sao paulo", "mexico", "argentina",
    "colombia", "apac", "latam", "philippines", "manila", "china", "beijing",
    "shanghai", "korea", "seoul", "taiwan", "hong kong", "dubai", "uae", "switzerland",
    "sweden", "stockholm", "denmark", "norway", "finland", "italy", "austria",
    # Added 2026-07-19: "San Jose, Costa Rica" matched the Bay Area gate on a bare
    # "san jose". Only unambiguous country names go here — no "georgia" (US state),
    # "cambridge"/"manchester"/"birmingham" (US cities), which would cost real jobs.
    "costa rica", "chile", "peru", "uruguay", "ecuador", "guatemala", "panama",
    "bolivia", "paraguay", "venezuela", "nicaragua", "honduras", "el salvador",
    "czech", "prague", "hungary", "budapest", "bulgaria", "serbia", "belgrade",
    "croatia", "slovakia", "slovenia", "lithuania", "latvia", "estonia", "ukraine",
    "belgium", "brussels", "luxembourg", "greece", "athens, gr", "cyprus", "malta",
    "iceland", "turkey", "istanbul", "egypt", "south africa", "nigeria", "kenya",
    "morocco", "malaysia", "kuala lumpur", "indonesia", "jakarta", "thailand",
    "bangkok", "vietnam", "new zealand", "auckland", "pakistan", "bangladesh",
    "sri lanka", "saudi", "qatar", "bahrain", "jordan", "lebanon", "armenia",
    "scotland", "edinburgh", "glasgow", "wales", "belfast",
)
US_REMOTE = (
    "united states", "usa", "u.s.", "us-remote", "remote - us", "remote, us",
    "remote (us", "us remote", "anywhere in the us", "nationwide", "remote us",
)
# Board hosts that only ever serve a non-US entity. A posting on one of these is
# presumed foreign unless its location says otherwise outright — "Remote" on a UK
# board means remote-in-the-UK. Caught 2026-07-19: three EU-board applies went out
# (Nscale Operations UK Ltd among them) because the location string alone looked clean.
FOREIGN_HOSTS = ("eu.greenhouse.io",)
# Locations that name the country and nothing else — no city, so nothing to commute to.
# "Remote" that is really "remote if you're on the other coast". He is Pacific.
COAST_LOCKED = (
    "us east", "u.s. east", "east coast", "us central", "midwest only",
    "est or cst", "et or ct", "eastern time", "central time", "eastern or central",
)
US_ONLY_LOCATIONS = frozenset({
    "united states", "united states of america", "us", "usa", "u s", "u s a",
    "us remote", "remote us", "remote united states", "united states remote",
    "nationwide", "anywhere in the us", "anywhere in the united states",
})


def _squash(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for exact location compares."""
    return " ".join("".join(c if c.isalnum() else " " for c in text).lower().split())


_STATE_IN_TITLE = re.compile(r",\s*([A-Z]{2})\b")
_CA_CODE = re.compile(r",\s*CA\b")
# Real USPS codes only. Accepting any bare [A-Z]{2} read "Remote, US" as a state, and
# did the same to "Inside Sales Representative, MM (PAM)" and "Analyst, AI".
_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DC DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
# Full state names, minus California. This generalises the ", ca" qualification on
# ambiguous city names: rather than chase every Bay city with a namesake elsewhere
# (Sunnyvale TX, Menlo Park NJ, Berkeley MO, Santa Clara UT, Mountain View AR,
# Hayward WI, Alameda NM), disqualify on the state the posting actually names.
# California, but not the Bay Area. A title naming one of these pins the role even
# when the location is filed country-wide: "Outside Sales Representative - Central
# Valley, CA" passed because the ", CA" satisfied the other-state check.
_CA_NON_BAY = (
    "central valley", "los angeles", "san diego", "sacramento", "orange county",
    "inland empire", "fresno", "bakersfield", "irvine", "san luis obispo",
    "santa barbara", "santa monica", "pasadena", "long beach", "anaheim",
    "riverside", "stockton", "modesto", "chico", "redding", "socal",
    "southern california", "central california",
)
_OTHER_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "colorado", "connecticut", "delaware",
    "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
)


def _other_state_only(location: str) -> bool:
    """True if the location names a non-CA state and never California.

    Multi-site postings that include California ("San Francisco, CA; New York, NY")
    are kept — he can take the Bay seat. Postings naming only somewhere else
    ("Livingston, NJ / New York, NY", "Sunnyvale, TX") are not reachable, and were
    slipping through on bare Bay Area city names that have namesakes out of state.
    """
    if not location:
        return False
    loc = location.lower()
    if "california" in loc or _CA_CODE.search(location):
        return False
    codes = {c for c in _STATE_IN_TITLE.findall(location)
             if c in _STATE_CODES and c != "CA"}
    return bool(codes) or any(s in loc for s in _OTHER_STATES)


def _names_other_state(title: str) -> bool:
    """True if the title pins the role to a non-California state, e.g.
    "Outside Sales Representative - Portland, OR". Such postings are routinely filed
    under a country-wide location, so the location string alone won't catch them."""
    t = title.lower()
    if any(c in t for c in _CA_NON_BAY) and not is_bay_area(title):
        return True
    if any(s != "CA" and s in _STATE_CODES for s in _STATE_IN_TITLE.findall(title)):
        return True
    # _STATE_IN_TITLE only sees a COMMA-prefixed code, so "BDR Texas Colorado" and
    # "SDR - Austin TX" both read as unpinned and inherit a country-wide location.
    # _OTHER_STATES already spells the names out for the location check; the title
    # deserves the same read. Names only, never bare codes: an uncommaed [A-Z]{2}
    # sweeps up OR, IN, ME and IT out of ordinary title text.
    return any(s in t for s in _OTHER_STATES) and "california" not in t


# --- pool + rotation state ---------------------------------------------------
def load_pool() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in POOL_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        platform, token = line.split(":", 1)
        platform, token = platform.strip().lower(), token.strip()
        if platform in KNOWN_PLATFORMS and token:
            out.append((platform, token))
    return out


DISCOVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discover_boards.py")


def expand_pool(pages: int, platforms: set[str]) -> int:
    """Enumerate more real boards from the URL index and append them to the pool.

    A thin sourcing run is a fact about how much of the ATS namespace we have enumerated,
    never a fact about the job market. So rather than report the pool exhausted, grow it.
    discover_boards.py resumes from data/discovery_state.json, so each call walks NEW index
    pages instead of re-probing boards we already hold.

    Returns the pool size afterwards, or 0 if discovery could not run -- callers treat 0 as
    "carry on with what we have", never as a reason to stop.
    """
    before = len(load_pool())
    print(
        f"[source] supply is short — enumerating {pages} more index pages per host "
        f"(discovery resumes where it left off)",
        file=sys.stderr,
    )
    try:
        r = subprocess.run(
            [sys.executable, DISCOVER, "--pages", str(pages),
             "--platforms", ",".join(sorted(platforms)), "--write"],
            capture_output=True, text=True,
        )
    except OSError as e:
        print(f"[source] discovery could not run: {e}", file=sys.stderr)
        return 0
    if r.returncode != 0:
        detail = (r.stderr.strip() or r.stdout.strip() or "no output")[:300]
        print(f"[source] discovery rc={r.returncode}: {detail}", file=sys.stderr)
        return 0
    after = len(load_pool())
    print(f"[source] pool {before} -> {after} boards (+{after - before})", file=sys.stderr)
    return after


def load_rotation() -> dict[tuple[str, str], dict]:
    state: dict[tuple[str, str], dict] = {}
    if ROTATION_PATH.exists():
        with ROTATION_PATH.open(newline="") as f:
            for row in csv.DictReader(f):
                key = (row.get("platform", ""), row.get("token", ""))
                state[key] = {
                    "last_mined": row.get("last_mined", ""),
                    "mines": int(row.get("mines") or 0),
                    "hits": int(row.get("hits") or 0),
                    "fails": int(row.get("fails") or 0),
                    "posts": int(row.get("posts") or 0),
                }
    return state


def save_rotation(state: dict[tuple[str, str], dict], keep: set | None = None) -> None:
    """Write rotation state atomically. open("w") truncates immediately, and a partial
    file reloads without error — load_rotation() happily parsed a half-written file as
    1,041 of 2,111 rows. Temp-file-and-rename makes the swap all-or-nothing.

    `keep` prunes boards that have left companies.txt, but it is only ever the
    CURRENT RUN's pool — which main() has already narrowed to --platforms and had the
    blocklist subtracted from. Pruning against it blindly deletes the history of every
    board this run wasn't looking at. That is not hypothetical: a breezy-scoped run on
    2026-08-08 15:13 cut data/board_rotation.csv from ~1,800 rows to 200 breezy-only
    rows, taking every greenhouse and lever `last_mined`/`hits`/`mines` with it. With
    the history gone, pick_boards() reads all 5,163 greenhouse boards as never-mined,
    loses the hit-rate weighting entirely, and samples near-uniformly over a pool that
    is mostly barren — which surfaces ~0 jobs and reads to the brain as "the market is
    exhausted". So only ever prune WITHIN the platforms this run actually mined.
    """
    ROTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    if keep is not None:  # drop boards that have left companies.txt
        scoped = {p for p, _ in keep}
        state = {k: v for k, v in state.items() if k in keep or k[0] not in scoped}
    tmp = ROTATION_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(ROTATION_HEADER)
        for (platform, token), v in sorted(state.items()):
            w.writerow([platform, token, v["last_mined"], v.get("mines", 0), v["hits"],
                        v["fails"], v.get("posts", 0)])
    os.replace(tmp, ROTATION_PATH)


def rotation_keep(platforms: set[str]) -> set[tuple[str, str]]:
    """Boards whose rotation history this run is allowed to prune against.

    The whole pool for the platforms being mined — deliberately NOT main()'s `pool`,
    which has also had the blocklist subtracted. Pruning against that would delete a
    blocked company's mining history, so unblocking it later would hand it back as
    never-mined with its productivity score reset to nothing.
    """
    return {b for b in load_pool() if b[0] in platforms}


def _age_hours(last_mined: str) -> float:
    """Hours since this board was last mined. Never-mined sorts as very stale."""
    if not last_mined:
        return 24 * 365
    try:
        then = datetime.fromisoformat(last_mined)
    except ValueError:
        return 24 * 365
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 3600)


def _is_live(s: dict) -> bool:
    """A pruned board gets one amnesty retry a month.

    Pruning was otherwise permanent and invisible: a board filtered out of `live` is
    never fetched, so `fails` can never reset to 0. A four-wave outage — or the 406
    throttling storm that prompted the ok/missing/throttled split — would delete a
    healthy board from the pool forever, with only a `pruned=N` count to show for it.

    Barren boards get no such amnesty, and want none: a board past BARREN_PER_MINE with
    zero lifetime hits is not transiently broken, it is an employer in another market.
    `hits` never decays, so `hits == 0` means it has never surfaced a job in any lane
    across every mine it has ever had — one matching posting, ever, exempts it forever.
    --stats names every pruned board, so this is never a silent disappearance.
    """
    if _posts_per_mine(s) >= BARREN_PER_MINE and not s.get("hits", 0):
        return False
    if s.get("fails", 0) < DEAD_AFTER:
        return True
    return _age_hours(s.get("last_mined", "")) > 24 * 30


def pick_boards(pool, state, k: int) -> list[tuple[str, str]]:
    """Weighted sample without replacement, weight = staleness × productivity × conversion.

    Efraimidis–Spirakis: key = random() ** (1/weight), take the top k. Stale boards
    win most of the time, but every live board keeps a real chance — that residual
    randomness is what makes two consecutive sessions see different companies.

    Productivity is the explore/exploit half: a board that has produced a matching job
    before is worth more than one mined three times for nothing (most big boards have
    no Bay-Area entry-level roles at all). Duds are demoted, never dropped — their
    postings turn over, and staleness eventually floats them back up.

    Conversion is the platform half, and it corrects a blind spot in productivity: hits
    counts jobs surfaced, so a board can look productive while none of its jobs can be
    applied to. See PLATFORM_CONVERSION.
    """
    live = [b for b in pool if _is_live(state.get(b, {}))]
    keyed = []
    for b in live:
        s = state.get(b, {})
        weight = (1.0 + _age_hours(s.get("last_mined", ""))) * PLATFORM_CONVERSION.get(b[0], 0.35)
        mines = s.get("mines", 0)
        if mines:
            # Hit RATE, not a hits>0 flag. As a flag the signal self-destructs: hits
            # never decays, so one lucky match pinned a board at x2 forever and made
            # the demotion branch unreachable for it. Simulated over the real pool,
            # 62% of boards were boosted by wave 100 and ~100% by wave 300, at which
            # point the multiplier cancels out and productivity becomes pure noise.
            weight *= 0.25 + min(2.0, 4.0 * s.get("hits", 0) / mines)
        keyed.append((random.random() ** (1.0 / weight), b))
    keyed.sort(reverse=True)
    return [b for _, b in keyed[:k]]


# --- filters -----------------------------------------------------------------
def classify(title: str, lane_filter: str | None) -> str | None:
    """Return the lane a title belongs to, or None if it isn't a target role."""
    t = f" {title.lower()} "
    # CLAUDE.md's HARD FIT FILTER says to drop pure SWE "UNLESS it says IT/Support/
    # SecOps". source.py had no such carve-out, so a bare "engineer" substring was
    # killing the best-converting lane he has: "Technical Support Engineer - University
    # Graduate 2026" (Verkada, San Mateo), "IT Help Desk Engineer" (C3 AI, Redwood
    # City), "Security Operations Engineer" (Pure Storage). 24 real postings per
    # 900-board sweep. Support titles are exempt from the engineer/developer family
    # only — every other exclusion still applies to them.
    if any(x in t for x in CODING_HEAVY):
        return None
    kill = EXCLUDE if not any(s in t for s in SUPPORT_SAFE) else _EXCLUDE_NO_ENG
    if any(x in t for x in kill):
        return None
    if _INTERNSHIP.search(t):  # word-boundary: don't kill "Internal"/"International"
        return None
    for lane, (_tier, keywords) in LANES.items():
        if lane_filter and lane != lane_filter:
            continue
        if any(k in t for k in keywords):
            return lane
    return None


def location_ok(location, title: str, url: str = "") -> bool:
    """Bay Area, or a fully-remote-US role (Jack, 2026-07-19). Everything else drops."""
    # Some boards return a posting with no location at all (null, not ""). Coerce it to
    # the empty string, which the "remote" branch below already handles. Without this one
    # null-location posting raised AttributeError and killed the WHOLE run, losing every
    # job sourced up to that point -- it only began showing up once the pool grew past the
    # original curated companies (2026-08-03).
    location = location or ""
    blob = f"{location} {title}".lower()
    if any(x in blob for x in NON_US):
        return False
    if _other_state_only(location):
        return False
    if any(c in blob for c in COAST_LOCKED):
        return False
    if any(h in url.lower() for h in FOREIGN_HOSTS):
        # Foreign board: the location has to name a US place itself. A bare "Remote"
        # is remote-in-that-country, and inheriting it is how the EU leak happened.
        # This only ADDS a requirement — the normal Bay/remote-US rule below still has
        # to pass. Returning True from here let an LA+NYC role through on the strength
        # of "United States" alone, which the ordinary path would have rejected.
        if not (is_bay_area(location) or any(x in location.lower() for x in US_REMOTE)):
            return False
    if is_bay_area(location, title):
        return True
    loc_l = location.lower()
    if ("remote" in loc_l or not loc_l.strip()) and any(x in blob for x in US_REMOTE):
        return True
    # A location that is ONLY a country name ("United States", "USA") and names no
    # city is a distributed role — treat it as remote-US even without the word
    # "remote". Anything with a city attached ("Los Angeles, ..., United States")
    # falls through to the rules above, which is what rejects office-bound roles.
    # The title still gets a look: "Outside Sales Representative - Portland, OR" is
    # posted with location "United States" but is plainly an Oregon territory role.
    if _squash(location) in US_ONLY_LOCATIONS and not _names_other_state(title):
        return True
    return False


def load_blocked_companies() -> set[str]:
    """Companies that blocked us on a specific ATS (data/blocklist.txt). Skipped at
    source time so a wave never re-opens a wall we already hit."""
    blocked: set[str] = set()
    if not BLOCKLIST_PATH.exists():
        return blocked
    for line in BLOCKLIST_PATH.read_text().splitlines():
        parts = [p.strip().lower() for p in line.split("\t") if p.strip()]
        if len(parts) >= 2:
            blocked.add(parts[1])
    return blocked


def load_known_urls() -> set[str]:
    known: set[str] = set()
    for path in (APPLIED_PATH, SEEN_PATH, RETRY_PATH):
        if not path.exists():
            continue
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                u = _norm(row.get("url", ""))
                if u:
                    known.add(u)
    return known


def _dead_urls() -> set[str]:
    """URLs that must never be re-served: already applied, or retired for cause.

    Deliberately does NOT include seen.csv -- in backlog mode seen.csv is the *source*,
    so the usual load_known_urls() dedup would filter out every candidate row.
    """
    out: set[str] = set()
    for name in ("applied.csv", "quarantine.csv", "retry_quarantine.csv", "retry.csv"):
        p = ROOT / "data" / name
        if not p.exists():
            continue
        try:
            with p.open(newline="") as f:
                for row in csv.DictReader(f):
                    u = _norm(row.get("url", ""))
                    if u:
                        out.add(u)
        except (OSError, csv.Error):
            continue
    return out


# Disposition notes the brain appended to the title instead of to a status column, e.g.
# "Salesforce BA - expired", "InfoSec GRC Analyst - no openings", "SDR USA - 410 gone".
# 889 of 3,837 seen.csv rows carry one. Re-serving those hands back work already triaged
# and thrown away, and no HTTP check catches them (see _alive). Word-boundaried on purpose:
# a bare "dead" substring also matches "deadline" -- verified against every real title in
# seen.csv, this pattern flags none of them.
DEAD_MARKER_RE = re.compile(
    r"\b(expired|dead|closed|gone|removed|unavailable|404|410)\b"
    r"|no longer|no openings|not found|not hiring",
    re.I,
)

# The other half of the same habit. DEAD_MARKER_RE catches the posting that VANISHED;
# this catches the posting that is still up and was judged unwinnable -- "SDR - Atlanta GA
# not Bay Area", "requires 2+ years experience", "too senior", "LinkedIn required". A wave
# already paid to open each of these, read it, and write down why it was a no; re-serving
# them buys that same verdict a second time.
#
# 502 of the 4,311 rows in seen.csv carry one (measured 2026-08-09). Every one of those 502
# was read by eye before this shipped, including all 58 that carry no " - " note separator
# and so could plausibly have been a real ATS title: none was. The risk here is a genuine
# posting titled "...Clearance Required", which this drops -- correctly, since the
# candidate holds none. Keep it anchored to phrases a recruiter would not put in a title.
REJECT_NOTE_RE = re.compile(
    r"\bnot\s+(?:the\s+)?(?:bay area|bay|remote|us\b|u\.s\.|usa\b)"
    r"|\b\d{1,2}\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:yrs?|years?)\b"
    r"|\btoo senior\b|\brequires?\b|\brequired\b|\bneeds\b|\bmust have\b"
    r"|\bclearance\b|\bts/sci\b|\bhard-?avoid\b|\bcaptcha\b",
    re.I,
)


# Which ATS a posting URL lives on. pick_boards() always knows the platform of a board it
# just mined; the SERVING path does not -- --backlog rows come out of seen.csv, which
# records a bare URL. Without this, PLATFORM_CONVERSION governs which boards get mined and
# then stops mattering, so a wave drawn from the backlog fills up with Ashby and Workable
# links at exactly the rate history happened to deposit them there.
_HOST_PLATFORM = (
    ("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
    ("breezy.hr", "breezy"), ("workable.com", "workable"), ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"), ("smartrecruiters.com", "smartrecruiters"),
    ("myworkdayjobs.com", "workday"), ("workday.com", "workday"), ("icims.com", "icims"),
    ("linkedin.com", "linkedin"),
)
# Platforms where the SUBMIT button has never once produced an application, measured
# 2026-08-09 over the full 4,311-row seen.csv against applied.csv:
#
#   greenhouse 659/2897 (22.7%)   breezy 36/152 (23.7%)   lever 41/511 (8.0%)
#   ashby      13/239   ( 5.4%)   workable 0/147          smartrecruiters 0/7
#   linkedin   0/7
#
# Workable's 0-for-147 is the one with a real sample behind it, and it agrees with what the
# waves reported: hCaptcha/Turnstile blocks the submit regardless of source IP, so a
# residential tunnel does not help. The others join it on the same evidence (Tier 3
# HARD-AVOID in CLAUDE.md) with smaller samples. Ashby is deliberately NOT here -- 13 did
# land, so it stays servable and simply sorts last via PLATFORM_CONVERSION.
#
# This is a serving filter, not a discovery one: the rows stay in seen.csv, so removing a
# name here is all it takes to start offering them again if a wall ever comes down.
SUBMIT_BLOCKED = frozenset({"workable", "smartrecruiters", "workday", "icims", "linkedin"})


def platform_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    for needle, name in _HOST_PLATFORM:
        if needle in host:
            return name
    # A Greenhouse board wearing a company skin (brex.com/careers/?gh_jid=...). These
    # convert like Greenhouse because they ARE Greenhouse.
    return "greenhouse" if "gh_jid=" in url else "other"


def _alive(url: str) -> bool:
    """Is this posting still open?

    Status code alone is not enough. A filled Greenhouse job answers **200** and quietly
    redirects to the board root -- verified 2026-08-04:

        GET job-boards.greenhouse.io/appian/jobs/7547392
          -> 200, final url job-boards.greenhouse.io/appian?error=true

    so the real signal is whether the job id survived the redirect. A 404/410 is likewise
    gone. Everything else -- 403 from a WAF, 429 from rate limiting, a timeout -- counts as
    alive: dropping a live job to avoid handing over a dead one is the wrong trade, and the
    brain learns the truth in one page load either way.
    """
    job_id = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    req = urllib.request.Request(url, headers={"User-Agent": _UA},
                                 method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 400:
                return False
            final = r.url or ""
            if "error=true" in final:
                return False
            # Redirected somewhere that no longer names this posting => it is gone.
            return not (job_id and job_id not in final)
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        return True          # network noise says nothing about the posting


# --- verify -------------------------------------------------------------------
# Measured over the 887 attempts logged since 2026-08-04: the brain opened a browser on
# 653 jobs it then threw away, and 89% of those went for three reasons that were all
# knowable without a browser — wrong location (38%), posting already closed (27%),
# experience band out of reach (24%). Each one cost a navigate + snapshot + read cycle,
# and at ~222 tool calls per application that waste IS the throughput ceiling.
#
# So every row is verified against the ATS's own single-job API before it is printed.
# One small JSON request replaces one browser page load, so this REDUCES total traffic
# rather than adding to it — and it is what finally lets the backlog path filter on a
# real location instead of the "?" placeholder it used to emit.
VERIFY_CACHE_PATH = ROOT / "data" / "verify_cache.json"
VERIFY_TTL_H = 24
# Threshold, not a guess at the ceiling: profile.yaml records no professional experience,
# and EXCLUDE already lets "Level II" (1-2 years) through on purpose. A posting whose
# LOWEST stated requirement is 4+ years is out of reach. Raise with --max-years.
# 3, not 4 (Jack, 2026-09-18): the first live cyber wave skipped eleven postings in one
# hour for seniority, and the brain's reasons name 3+ years as too senior every time
# ("3+ years required, too senior", "3-5 years hands-on required"). Verify was set one
# year looser than the brain, so each of those rows was printed, opened in the browser
# and thrown away at 5-10 minutes apiece. The min-years rule still protects the
# "2+ required, 5+ preferred" posting: only the smallest number on the page counts.
DEFAULT_MAX_YEARS = 3
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus|or more)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*years?"
    # "2+ years required" and "3 years minimum" name a bar as plainly as "years of
    # experience" does, and job descriptions use them constantly. The trailing context
    # stays mandatory: without it a bare "years" matches "5 years ago we were founded".
    r"[^.;)]{0,40}?(?:experience|exp\b|background|working|in\s+a\s+similar"
    r"|required|require[sd]?|minimum|preferred)",
    re.I,
)
_TAGS_RE = re.compile(r"<[^>]+>")


def _posting_api(url: str) -> tuple[str, str] | None:
    """Map a posting URL to its single-job API endpoint, or None if we can't.

    Greenhouse vanity careers domains (brex.com/careers/..., navan.com/...) carry the
    job id in ?gh_jid= but never the board token, so they are unverifiable here and
    fall through to the plain HTTP check. That is the documented 60-odd hosts from
    PLATFORM_CONVERSION, not a long tail worth chasing.
    """
    p = urllib.parse.urlsplit(url)
    host, path = p.netloc.lower(), p.path.strip("/").split("/")
    if "greenhouse.io" in host and len(path) >= 3 and path[1] == "jobs":
        return "greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{path[0]}/jobs/{path[2]}"
    if "lever.co" in host and len(path) >= 2:
        return "lever", f"https://api.lever.co/v0/postings/{path[0]}/{path[1]}"
    return None


def _strip_html(raw: str) -> str:
    """Markup out, text in - unescaping FIRST.

    Greenhouse serves its `content` html-ESCAPED (&lt;div&gt;...), so stripping tags
    before unescaping leaves every tag behind as literal text. Measured on a real
    posting: 20,016 chars of escaped markup vs 9,398 of prose. That matters because
    _YEARS_RE requires "years" and "experience|required|minimum" within 40 characters
    of each other, and a "&lt;/span&gt;&lt;span ...&gt;" between them defeats it - which is
    why the experience-band gate was passing greenhouse postings that state a bar.
    """
    return _TAGS_RE.sub(" ", html.unescape(raw or "")).replace("\xa0", " ")


def _min_years_required(text: str) -> int | None:
    """Smallest "N years experience" figure in the body, or None if it names none.

    The minimum on purpose, never the maximum: a posting that says "2+ years required,
    5+ preferred" is open to him and must survive. Only when even the smallest number
    on the page is out of reach is the posting genuinely unreachable.
    """
    hits = [int(m.group(1)) for m in _YEARS_RE.finditer(text or "")]
    hits = [h for h in hits if 0 < h <= 20]
    return min(hits) if hits else None


def _load_verify_cache() -> dict:
    try:
        raw = json.loads(VERIFY_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - VERIFY_TTL_H * 3600
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("ts", 0) > cutoff}


def _save_verify_cache(cache: dict) -> None:
    try:
        VERIFY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = VERIFY_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, VERIFY_CACHE_PATH)
    except OSError:
        pass


_BREEZY_BOARDS: dict[str, dict | None] = {}


def _breezy_verify(url: str) -> dict | None:
    """Verify a breezy posting against its board listing, or None if not breezy.

    Breezy does NOT 404 a closed posting: the page answers 200 and says "this position
    has been closed", so _alive() passes it and the brain spends a browser load finding
    out. Measured on the first live cyber wave, which opened four such pages in 45
    seconds (Ceresti Health, Sourcefit, Continued, Zero Hash — all first seen 2026-08-01).
    The board's own /json is authoritative and drops closed postings, so absence from it
    is the liveness signal. One request per board, cached for the run.
    """
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower()
    if not host.endswith("breezy.hr"):
        return None
    token = host.split(".")[0]
    job_id = p.path.strip("/").split("/")[-1]
    if not token or not job_id:
        return None
    if token not in _BREEZY_BOARDS:
        try:
            _BREEZY_BOARDS[token] = {"jobs": _get(f"https://{token}.breezy.hr/json")}
        except Exception:  # noqa: BLE001 — transport noise says nothing about the posting
            _BREEZY_BOARDS[token] = None
    board = _BREEZY_BOARDS[token]
    if board is None:
        return {"alive": True, "location": None, "years": None, "reason": ""}
    for j in board["jobs"]:
        if job_id not in (j.get("url") or "") and job_id != str(j.get("id") or ""):
            continue
        loc = j.get("location") or {}
        bits = [loc.get("city"), (loc.get("state") or {}).get("name"),
                (loc.get("country") or {}).get("name")]
        where = ", ".join(b for b in bits if b) or (loc.get("name") or "")
        if loc.get("is_remote"):
            where = f"Remote, {where}" if where else "Remote"
        return {"alive": True, "location": where or None,
                "years": _min_years_required(_strip_html(j.get("description", "") or "")),
                "reason": ""}
    return {"alive": False, "location": None, "years": None,
            "reason": "gone from the breezy board listing"}


def _verify_one(url: str) -> dict:
    """{alive, location, years, reason} for one posting, from the ATS API where possible.

    Unknown beats a guess everywhere: any transport failure returns alive=True with a
    null location, so a bad minute on the API can never delete a live job. Only an
    explicit 404/410, a redirect off the posting, or a deadline in the past is fatal.
    """
    breezy = _breezy_verify(url)
    if breezy is not None:
        return breezy
    api = _posting_api(url)
    if not api:
        return {"alive": _alive(url), "location": None, "years": None, "reason": ""}
    platform, endpoint = api
    try:
        req = urllib.request.Request(endpoint, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return {"alive": False, "location": None, "years": None, "reason": "posting 404"}
        return {"alive": True, "location": None, "years": None, "reason": ""}
    except Exception:  # noqa: BLE001 — transport noise says nothing about the posting
        return {"alive": True, "location": None, "years": None, "reason": ""}

    if platform == "greenhouse":
        loc = (j.get("location") or {}).get("name", "")
        body = _strip_html(j.get("content", ""))
        deadline = j.get("application_deadline")
    else:
        loc = (j.get("categories") or {}).get("location", "")
        body = _strip_html(j.get("description", "") or "") + " " + " ".join(
            _strip_html(s.get("text", "") + " " + " ".join(
                (it if isinstance(it, str) else it.get("text", ""))
                for it in (s.get("content") or [])))
            for s in (j.get("lists") or [])
        )
        deadline = None
    if isinstance(deadline, dict):
        deadline = deadline.get("date")
    if deadline:
        try:
            when = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < datetime.now(timezone.utc):
                return {"alive": False, "location": loc, "years": None,
                        "reason": "application deadline passed"}
        except ValueError:
            pass
    return {"alive": True, "location": loc, "years": _min_years_required(body), "reason": ""}


def verify_rows(rows: list[dict], want: int, max_years: int,
                enabled: bool = True, label: str = "") -> list[dict]:
    """Filter rows down to `want` postings that are open, in-location and in-band.

    Stops as soon as it has enough, so a healthy queue costs only as many requests as
    it serves. Four workers rather than the sixteen `_alive` used: that 16-thread burst
    across 120 URLs every wave is what put the Hetzner IP behind Greenhouse's WAF on
    2026-08-08 and returned 406 for every request from the box for hours, which cost
    four consecutive waves their entire output.
    """
    if not enabled:
        return rows[:want]
    cache = _load_verify_cache()
    kept: list[dict] = []
    drops: dict[str, int] = {}
    checked = 0

    def check(r: dict) -> tuple[dict, dict]:
        key = _norm(r["url"])
        hit = cache.get(key)
        if hit:
            return r, hit
        time.sleep(0.15 + random.random() * 0.2)   # ~4-6 req/s ceiling across 4 workers
        v = _verify_one(r["url"])
        v["ts"] = time.time()
        cache[key] = v
        return r, v

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r, v in ex.map(check, rows):
            checked += 1
            if not v.get("alive"):
                drops[v.get("reason") or "closed"] = drops.get(v.get("reason") or "closed", 0) + 1
                continue
            loc = v.get("location")
            if loc and not location_ok(loc, r["title"], r["url"]):
                drops["wrong location"] = drops.get("wrong location", 0) + 1
                continue
            yrs = v.get("years")
            if max_years and yrs is not None and yrs >= max_years:
                drops[f"{max_years}+ years required"] = drops.get(f"{max_years}+ years required", 0) + 1
                continue
            if loc:
                r["location"] = loc
            kept.append(r)
            if len(kept) >= want:
                break

    _save_verify_cache(cache)
    if drops:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(drops.items(), key=lambda x: -x[1]))
        print(f"[source] verify{label}: {checked} checked, {len(kept)} serving, "
              f"dropped — {detail}", file=sys.stderr)
    return kept


def backlog_rows(args, blocked: set[str]) -> list[dict]:
    """Never-applied, still-in-lane rows from seen.csv, tier 1 first, verified."""
    if not SEEN_PATH.exists():
        print("[source] --backlog: no data/seen.csv", file=sys.stderr)
        return []
    dead = _dead_urls()
    rows: list[dict] = []
    seen_urls: set[str] = set()
    total = 0
    n_marked = 0
    n_offsite = 0
    n_walled = 0
    with SEEN_PATH.open(newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            url = row.get("url") or ""
            key = _norm(url)
            if not key or key in dead or key in seen_urls:
                continue
            company = row.get("company") or "?"
            if company.lower() in blocked:
                continue
            platform = platform_of(url)
            if platform in SUBMIT_BLOCKED:
                n_walled += 1
                continue
            title = row.get("title") or ""
            if DEAD_MARKER_RE.search(title) or REJECT_NOTE_RE.search(title):
                n_marked += 1
                continue
            lane = classify(title, args.lane)
            if not lane:
                continue
            # Rows written before seen.csv gained a location column carry none, so the
            # title check is all that can run here -- verify_rows() below fetches the
            # real location from the ATS and applies the full location_ok() gate. Rows
            # that DO carry one are filtered now, which is strictly cheaper: it costs no
            # request at all and keeps them out of the verify budget.
            location = row.get("location") or ""
            if location and not location_ok(location, title, url):
                n_offsite += 1
                continue
            if any(x in title.lower() for x in NON_US) or _names_other_state(title):
                n_offsite += 1
                continue
            seen_urls.add(key)
            tier = _tier(lane)
            early = any(sig in f" {title.lower()} " for sig in EARLY_SIGNALS)
            rows.append({
                # Within a tier, the platform that actually converts goes first. Negated
                # so higher conversion sorts earlier; unknown hosts sit at Lever's rate.
                "sort": (tier - (0.5 if early else 0),
                         -PLATFORM_CONVERSION.get(platform, 0.35), random.random()),
                "url": url, "company": company, "title": title,
                "location": location or "?", "lane": lane, "board": None,
                "first_seen": (row.get("first_seen") or "")[:10],
            })

    rows.sort(key=lambda r: r["sort"])
    tiers = {}
    for r in rows:
        tiers[_tier(r["lane"])] = tiers.get(_tier(r["lane"]), 0) + 1
    print(f"[source] --backlog: {total} seen, {n_marked} already triaged in their title, "
          f"{n_offsite} pinned to a disallowed location by title, {n_walled} on a platform "
          f"that has never accepted a submit, {len(rows)} never-applied "
          f"and in-lane (by tier: {dict(sorted(tiers.items()))})",
          file=sys.stderr)

    # Same lifetime employer ceiling as the mining path -- a backlog this size is easily
    # dominated by a handful of big boards otherwise.
    per_company = prior_applications({r["company"] for r in rows})
    capped = []
    for r in rows:
        if per_company.get(r["company"], 0) >= args.per_company:
            continue
        per_company[r["company"]] = per_company.get(r["company"], 0) + 1
        capped.append(r)
    if len(capped) < len(rows):
        print(f"[source] --backlog: {len(rows) - len(capped)} dropped at the "
              f"{args.per_company}-application employer ceiling", file=sys.stderr)

    # Verify a slice rather than all of it: checking 2,500 URLs to print 60 is minutes of
    # wall clock for nothing. The headroom is 6x rather than the old 4x because verify
    # now rejects on location and experience band as well as liveness, so a larger share
    # of the batch is spent before `args.n` survivors are found.
    batch = capped[: max(args.n * 6, args.n)]
    return verify_rows(batch, args.n, args.max_years,
                       enabled=not args.no_verify, label=" --backlog")


def prior_applications(tokens: set[str]) -> dict[str, int]:
    """How many applications each board has ALREADY received, from applied.csv.

    Matched on the URL, not the company column: the column is free text the brain wrote
    ("bestversionmedia" vs "Cognizo" vs "Clarke Consulting"), whereas every posting URL
    carries its board token either as a path segment (greenhouse, lever) or as the
    subdomain (breezy, recruitee, teamtailor). Both shapes are checked.
    """
    counts = {t: 0 for t in tokens}
    if not tokens or not APPLIED_PATH.exists():
        return counts
    # Two ways to recognise the same employer, because neither alone is enough. The URL
    # carries the token on most boards, but Greenhouse serves vanity careers domains --
    # Brex's 26 applications are all www.brex.com/careers/..., with "brex" nowhere in the
    # path -- so those rows are only findable through the company column. That column is
    # in turn free text, hence the punctuation-insensitive compare ("Clarke Consulting"
    # against the board token "clarke-consulting").
    def ident(v: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (v or "").lower())

    by_name: dict[str, list[str]] = {}
    for t in tokens:
        by_name.setdefault(ident(t), []).append(t)
    with APPLIED_PATH.open(newline="") as f:
        for row in csv.DictReader(f):
            u = (row.get("url") or "").lower()
            matched = set(by_name.get(ident(row.get("company")), ()))
            for t in tokens:
                if u and (f"/{t}/" in u or f"//{t}." in u):
                    matched.add(t)
            for t in matched:
                counts[t] += 1
    return counts


# --- board fetchers ----------------------------------------------------------
def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch(board: tuple[str, str]) -> tuple[tuple[str, str], list[dict] | None, str]:
    """Return (board, postings, status).

    status is "ok" | "missing" | "throttled". The distinction matters: a board that
    404s is genuinely gone and should accrue `fails` toward auto-pruning, but a 406 or
    429 means the *API* is refusing us right now and says nothing about the board.
    Greenhouse starts returning 406 under sustained load — observed 2026-07-19, where
    63 perfectly good boards all 406'd in one burst and every one of them answered 200
    minutes later. Counting those as failures would have pruned them from rotation
    after four throttled waves, silently shrinking the pool with no signal.
    """
    platform, token = board
    for attempt in range(2):
        try:
            return board, _fetch_once(platform, token), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return board, None, "missing"
            if attempt == 0:
                time.sleep(1.5 + random.random())  # throttled or 5xx — back off once
                continue
            return board, None, "throttled"
        except Exception:  # noqa: BLE001 — timeouts/DNS: transient, never a prune signal
            if attempt == 0:
                time.sleep(1.5 + random.random())
                continue
            return board, None, "throttled"
    return board, None, "throttled"


def _fetch_once(platform: str, token: str) -> list[dict]:
    """One API call, normalised to {title, location, url}. Raises on any failure."""
    if platform == "greenhouse":
        jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs").get("jobs", [])
        return [{
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
        } for j in jobs]
    if platform == "lever":
        jobs = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
        return [{
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
        } for j in jobs]
    if platform == "breezy":
        # https://<token>.breezy.hr/json -- a bare list of postings. Location is a nested
        # object; flatten city/state/country into one string and prepend "Remote" when the
        # posting is remote, so location_ok sees the same shape it gets from the others.
        jobs = _get(f"https://{token}.breezy.hr/json")
        out = []
        for j in jobs:
            loc = j.get("location") or {}
            bits = [loc.get("city"),
                    (loc.get("state") or {}).get("name"),
                    (loc.get("country") or {}).get("name")]
            where = ", ".join(b for b in bits if b) or (loc.get("name") or "")
            if loc.get("is_remote"):
                where = f"Remote, {where}" if where else "Remote"
            out.append({
                "title": j.get("name", ""),
                "location": where,
                "url": j.get("url", ""),
            })
        return out
    if platform == "workable":
        jobs = _get(
            f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
        ).get("jobs", [])
        out = []
        for j in jobs:
            bits = [j.get("city"), j.get("state"), j.get("country")]
            where = ", ".join(b for b in bits if b)
            if j.get("telecommuting"):
                where = f"Remote, {where}" if where else "Remote"
            out.append({
                "title": j.get("title", ""),
                "location": where,
                "url": j.get("shortlink") or j.get("url") or j.get("application_url") or "",
            })
        return out
    if platform == "recruitee":
        # https://<token>.recruitee.com/api/offers/ -- city/state_name/country_code are
        # separate fields, and `location` collapses to the bare string "Remote job" on
        # remote roles, throwing the city away. Rebuild it the way breezy is handled so
        # location_ok sees a real place even when the posting is remote.
        jobs = _get(f"https://{token}.recruitee.com/api/offers/").get("offers", [])
        out = []
        for j in jobs:
            if j.get("status") != "published":
                continue
            # `country` ("United States"), not `country_code` ("US"): US_REMOTE matches
            # on the spelled-out name, so the code form would fail a genuinely remote-US
            # posting that names no city.
            bits = [j.get("city"), j.get("state_name"), j.get("country")]
            where = ", ".join(b for b in bits if b) or (j.get("location") or "")
            if j.get("remote"):
                where = f"Remote, {where}" if where else "Remote"
            out.append({
                "title": j.get("title", ""),
                "location": where,
                "url": j.get("careers_apply_url") or j.get("careers_url") or "",
            })
        return out
    if platform == "teamtailor":
        # https://<token>.teamtailor.com/jobs.json -- JSON Feed, with a schema.org
        # JobPosting embedded per item. jobLocation is a dict on most boards and a list on
        # multi-site ones, so both shapes are handled rather than assumed.
        items = _get(f"https://{token}.teamtailor.com/jobs.json").get("items", [])
        out = []
        for it in items:
            jl = (it.get("_jobposting") or {}).get("jobLocation") or {}
            if isinstance(jl, list):
                jl = jl[0] if jl else {}
            addr = (jl.get("address") or {}) if isinstance(jl, dict) else {}
            # Teamtailor gives an ISO code; spell US out for the same reason as above.
            country = (addr.get("addressCountry") or "")
            if country.upper() in ("US", "USA"):
                country = "United States"
            bits = [addr.get("addressLocality"), addr.get("addressRegion"), country]
            out.append({
                "title": it.get("title", ""),
                "location": ", ".join(b for b in bits if b),
                "url": it.get("url", ""),
            })
        return out
    if platform != "ashby":
        raise ValueError(f"unknown platform in companies.txt: {platform!r}")
    jobs = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").get("jobs", [])
    return [{
        "title": j.get("title", ""),
        "location": str(j.get("location") or ""),
        "url": j.get("jobUrl") or j.get("applyUrl") or "",
    } for j in jobs]


# --- main --------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Rotating live-board job sourcer.")
    ap.add_argument("--n", type=int, default=30, help="max jobs to print (default 30)")
    # 400 boards is ~7s at 16 workers — mining is cheap, so breadth is nearly free.
    # (32 workers is slower, not faster: the board APIs start rate-limiting.)
    ap.add_argument("--boards", type=int, default=400, help="boards to mine this run (default 400)")
    ap.add_argument("--lane", choices=sorted(LANES), help="restrict to one lane")
    ap.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS),
                    help="comma-separated: greenhouse,lever[,ashby] — Ashby is Tier 3 "
                         "HARD-AVOID and off by default; pass it only when desperate")
    ap.add_argument("--max-years", type=int, default=DEFAULT_MAX_YEARS,
                    help=f"drop postings whose lowest stated experience requirement is "
                         f"this many years or more (default {DEFAULT_MAX_YEARS}); "
                         f"0 disables the experience gate")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the ATS single-job verify pass (liveness/location/"
                         "experience). Faster, but restores the old behaviour where the "
                         "brain discovers all three in a browser")
    ap.add_argument("--per-company", type=int, default=4,
                    help="lifetime cap on applications to any one company, counting those "
                         "already in applied.csv (default 4)")
    ap.add_argument("--expand-below", type=int, default=15,
                    help="if fewer than this many jobs surface, enumerate more boards so "
                         "the next wave has fresh ones (default 15)")
    ap.add_argument("--expand-pages", type=int, default=20,
                    help="index pages per host to walk when expanding (default 20)")
    ap.add_argument("--no-expand", action="store_true",
                    help="never grow the pool automatically")
    ap.add_argument("--backlog", action="store_true",
                    help="mine nothing; re-serve jobs already in seen.csv that were never "
                         "applied to, tier 1 first, each liveness-checked. Measured "
                         "2026-08-04: 3,837 seen -> 1,820 servable, 893 of them tier 1, "
                         "after dropping 885 the brain had already marked dead in the "
                         "title and 147 whose title pins them outside the Bay Area.")
    ap.add_argument("--include-seen", action="store_true", help="don't dedup against seen/applied")
    ap.add_argument("--no-rotate", action="store_true", help="don't update rotation state")
    ap.add_argument("--stats", action="store_true", help="print rotation state and exit")
    args = ap.parse_args(argv[1:])

    # Backlog mode short-circuits everything below: no platforms, no pool, no rotation.
    if args.backlog:
        rows = backlog_rows(args, load_blocked_companies())
        for r in rows:
            print("\t".join([r["url"], r["company"], r["title"], r["location"], r["lane"]]))
        if not rows:
            print("[source] --backlog empty — every sourced job has been applied to or "
                  "closed. Run without --backlog to mine fresh boards.", file=sys.stderr)
            return 1
        lanes = ", ".join(sorted({r["lane"] for r in rows}))
        oldest = min((r["first_seen"] for r in rows if r["first_seen"]), default="?")
        print(f"[source] {len(rows)} backlog jobs — lanes: {lanes}; oldest first seen "
              f"{oldest}. No boards mined, rotation untouched.", file=sys.stderr)
        return 0

    wanted = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    unknown = wanted - set(KNOWN_PLATFORMS)
    if unknown:
        print(f"unknown platform(s): {sorted(unknown)} — expected "
              f"{'/'.join(KNOWN_PLATFORMS)}",
              file=sys.stderr)
        return 2
    pool = [b for b in load_pool() if b[0] in wanted]
    state = load_rotation()
    if not pool:
        print(f"no boards for platforms={sorted(wanted)}", file=sys.stderr)
        return 1

    if args.stats:
        live = [b for b in pool if _is_live(state.get(b, {}))]
        never = [b for b in live if not state.get(b, {}).get("last_mined")]
        dead = [b for b in pool if not _is_live(state.get(b, {}))]
        print(f"pool={len(pool)} live={len(live)} never_mined={len(never)} pruned={len(dead)}")
        for b in dead:  # name them — a silent prune is indistinguishable from a bug
            s = state.get(b, {})
            why = ("barren" if _posts_per_mine(s) >= BARREN_PER_MINE and not s.get("hits", 0)
                   else "dead")
            print(f"  PRUNED {b[0]}:{b[1]}  {why}  fails={s.get('fails', 0)} "
                  f"posts={s.get('posts', 0)} hits={s.get('hits', 0)} "
                  f"last_mined={s.get('last_mined') or 'never'}")
        ranked = sorted(live, key=lambda b: -_age_hours(state.get(b, {}).get("last_mined", "")))
        for b in ranked[:15]:
            age = _age_hours(state.get(b, {}).get("last_mined", ""))
            print(f"  {b[0]}:{b[1]}  stale={age/24:.1f}d  hits={state.get(b, {}).get('hits', 0)}")
        return 0

    # Drop blocked companies before sampling: they were still fetched every rotation,
    # always yielded nothing, and consumed a slot they can never repay.
    blocked_now = load_blocked_companies()
    if blocked_now:
        pool = [b for b in pool if b[1].lower() not in blocked_now]
    boards = pick_boards(pool, state, args.boards)

    # Under-filling the sample means the pool ran out of boards that are stale enough to
    # be worth mining -- a supply problem we can fix right now, since boards discovered
    # this second have never been mined and are immediately eligible.
    expanded = False
    if len(boards) < args.boards and not args.no_expand:
        expanded = True
        if expand_pool(args.expand_pages, wanted):
            pool = [b for b in load_pool() if b[0] in wanted]
            if blocked_now:
                pool = [b for b in pool if b[1].lower() not in blocked_now]
            boards = pick_boards(pool, state, args.boards)

    if not boards:
        print("no live boards in pool — check scripts/companies.txt", file=sys.stderr)
        return 1

    # 8 workers, not 16. On 2026-08-08 a wave's sourcing phase pushed ~170 requests at
    # boards-api.greenhouse.io in a burst and Greenhouse's WAF answered 406 for every
    # request from this IP — front-end included — for hours. Four consecutive waves then
    # applied to nothing. Halving concurrency costs a sourcing run seconds; the ban cost
    # a day of applications.
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch, boards))

    known = set() if args.include_seen else load_known_urls()
    blocked = load_blocked_companies()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, ok_boards, dead_boards, throttled_boards = [], 0, 0, 0

    for board, postings, status in results:
        entry = state.setdefault(
            board, {"last_mined": "", "mines": 0, "hits": 0, "fails": 0, "posts": 0})
        if status == "throttled":
            # The API refused us; the board is not implicated. Leave state untouched so
            # this costs the board neither a `fails` tick nor its staleness — it should
            # come straight back up in the next wave's sample.
            throttled_boards += 1
            continue
        if status == "missing":
            entry["fails"] += 1
            dead_boards += 1
            continue
        entry["fails"] = 0
        entry["last_mined"] = now
        entry["mines"] = entry.get("mines", 0) + 1
        # Volume, so _is_live() can tell a small board that happens to have nothing this
        # week from a 5,000-posting franchise chain that has never had anything at all.
        entry["posts"] = entry.get("posts", 0) + len(postings)
        ok_boards += 1
        if board[1].lower() in blocked:
            continue
        for p in postings:
            url, title = p["url"], p["title"]
            if not url or _norm(url) in known:
                continue
            lane = classify(title, args.lane)
            if not lane or not location_ok(p["location"], title, url):
                continue
            known.add(_norm(url))
            tier = _tier(lane)
            early = any(s in f" {title.lower()} " for s in EARLY_SIGNALS)
            rows.append({
                "sort": (tier - (0.5 if early else 0), random.random()),
                "url": url, "company": board[1], "title": title,
                "location": p["location"] or "?", "lane": lane,
                "board": board,
            })

    # Cap per company before truncating — one 700-posting board would otherwise eat the
    # whole wave, which is the crowding-out this script exists to fix.
    #
    # The cap counts applications ALREADY SENT to that company, not just the ones in this
    # run. Per-run alone let the same employer come back every wave: Brex ended up with 26
    # applications, Verkada 17, Figma 13. Seeding from applied.csv makes --per-company mean
    # what it says — a ceiling per employer — and pushes the surplus to companies that have
    # not heard from him yet.
    rows.sort(key=lambda r: r["sort"])
    per_company: dict[str, int] = prior_applications({r["company"] for r in rows})
    already_full = sum(1 for v in per_company.values() if v >= args.per_company)
    capped = []
    for r in rows:
        if per_company.get(r["company"], 0) >= args.per_company:
            continue
        per_company[r["company"]] = per_company.get(r["company"], 0) + 1
        capped.append(r)
    if already_full:
        print(f"[source] {already_full} boards skipped: already at the "
              f"{args.per_company}-application ceiling", file=sys.stderr)

    # Verify before truncating, not after: the board list API gives a title and a
    # location and nothing else, so a posting that closed last month or wants six years
    # of experience looks identical to a live entry-level one until the single-job API
    # is asked. Taking capped[:n] first and verifying second would hand back a short
    # wave every time, which is exactly the "pool is dry" report the brain kept filing.
    rows = verify_rows(capped[: max(args.n * 4, args.n)], args.n, args.max_years,
                       enabled=not args.no_verify)

    # Credit productivity from the jobs that actually reached the wave, not from every
    # pre-cap match. Measured at --boards 400: 99 hits were credited across 42 boards
    # while only 30 rows printed — 70% of the credit was for jobs nobody ever saw, and
    # the aggregator board lever:jobgether banked 40 hits while showing 3.
    # --lane and --include-seen deliberately skew what counts as a match, so they must
    # not write productivity back into shared state.
    if not (args.lane or args.include_seen):
        for r in rows:
            state[r["board"]]["hits"] += 1

    if not args.no_rotate:
        save_rotation(state, keep=rotation_keep(wanted))

    for r in rows:
        print("\t".join([r["url"], r["company"], r["title"], r["location"], r["lane"]]))

    if ok_boards == 0 or throttled_boards > ok_boards:
        print(
            f"WARNING: {ok_boards} boards answered, {throttled_boards} throttled, "
            f"{dead_boards} gone. This is an API problem, not an empty market — "
            f"re-run before concluding the wave is dry.",
            file=sys.stderr,
        )
        if not args.no_rotate:
            save_rotation(state, keep=rotation_keep(wanted))
        return 2

    # Healthy boards but a thin yield means the enumerated pool is too small for these
    # filters, not that the market is empty. --lane and --include-seen deliberately skew
    # the count, so they must not trigger this.
    if (len(rows) < args.expand_below and not args.no_expand and not expanded
            and not (args.lane or args.include_seen)):
        expand_pool(args.expand_pages, wanted)

    lanes_seen = ", ".join(sorted({r["lane"] for r in rows})) or "none"
    rotated = "Rotation updated; next run mines different boards." if not args.no_rotate \
        else "Rotation NOT updated (--no-rotate) — next run may mine these same boards."
    print(
        f"\n[source] {len(rows)} fresh jobs from {ok_boards} boards "
        f"({dead_boards} gone, {throttled_boards} throttled) — lanes: {lanes_seen}. {rotated}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
