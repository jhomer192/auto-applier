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

Pool lives in scripts/companies.txt (`platform:token`) — ~1,900 verified-live boards.
Add companies there; never hardcode them here.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.error
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
    "burlingame", "san bruno", "millbrae", "san carlos", "belmont, ca", "half moon bay",
    "east palo alto",
    "san jose", "santa clara", "sunnyvale", "mountain view", "cupertino", "milpitas",
    "los gatos", "campbell, ca", "saratoga, ca", "morgan hill", "gilroy",
    "oakland", "berkeley", "emeryville", "alameda", "fremont, ca", "hayward",
    "richmond, ca", "walnut creek", "pleasanton", "dublin, ca", "san ramon",
    "concord, ca", "union city, ca", "newark, ca", "castro valley", "san leandro",
    "pleasant hill", "danville, ca", "livermore", "martinez, ca", "lafayette, ca",
    "orinda", "albany, ca",
    "marin", "sausalito", "san rafael", "novato", "mill valley", "corte madera",
    "larkspur", "tiburon", "petaluma",
)


def is_bay_area(*texts: str) -> bool:
    """True iff any text names a Bay Area location. Strict: unknown/empty → False."""
    blob = " ".join(t for t in texts if t).lower()
    return bool(blob.strip()) and any(term in blob for term in BAY_TERMS)

POOL_PATH = ROOT / "scripts" / "companies.txt"
ROTATION_PATH = ROOT / "data" / "board_rotation.csv"
APPLIED_PATH = ROOT / "data" / "applied.csv"
SEEN_PATH = ROOT / "data" / "seen.csv"
RETRY_PATH = ROOT / "data" / "retry.csv"  # already queued for another attempt — don't re-surface
BLOCKLIST_PATH = ROOT / "data" / "blocklist.txt"  # "<ats_host>\t<company>\t<reason>"
# Tier 1 per CLAUDE.md SITE ROUTING — the only platforms that actually convert. Ashby
# blocks at submit regardless of IP (Tier 3 HARD-AVOID), so it is OFF by default: sourcing
# Ashby jobs just fills a wave with applications that can never land.
DEFAULT_PLATFORMS = ("greenhouse", "lever")
ROTATION_HEADER = ["platform", "token", "last_mined", "mines", "hits", "fails"]
DEAD_AFTER = 4  # consecutive fetch failures before a board is dropped from rotation

# --- lanes -------------------------------------------------------------------
# Ordered by conversion odds for a zero-experience candidate (see CLAUDE.md
# "Sourcing priority"). Tier 1 lanes are ranked first in the output; within a tier
# the order is random so no single lane monopolises the top of every wave.
LANES: dict[str, tuple[int, tuple[str, ...]]] = {
    "security": (1, (
        "security analyst", "soc analyst", "soc ", "cyber", "grc", "governance risk",
        "information security", "infosec", "security operations", "threat",
        "incident response", "vulnerability", "compliance analyst", "risk analyst",
        "security compliance", "trust and safety", "trust & safety",
        # Security-consultancy titles. Zach applied to three Coalfire FedRAMP roles on
        # 2026-07-19 that source.py couldn't see — it found them only by browsing the
        # board directly. These are squarely in-lane for a CompTIA-certified candidate.
        "fedramp", "soc 2", "iso 27001", "security assessment", "security consultant",
        "compliance consultant", "identity and access", "iam analyst",
    )),
    "sdr": (1, (
        "sales development", "business development representative", "bdr", "sdr",
        "account development", "sales representative", "inside sales",
        "market development representative",
    )),
    "it": (2, (
        "it support", "help desk", "helpdesk", "service desk", "desktop support",
        "technical support", "it specialist", "it associate", "systems support",
    )),
    "people": (2, (
        "recruiting coordinator", "recruiting associate", "talent coordinator",
        "people operations", "people coordinator", "hr coordinator", "hr associate",
        "onboarding specialist",
    )),
    "finance": (3, (
        "finance associate", "financial analyst", "risk operations", "aml",
        "fraud analyst", "fraud operations", "payments operations", "kyc",
        "compliance associate", "billing", "accounts payable", "accounts receivable",
    )),
    "cs": (3, (
        "customer success", "customer support", "customer experience",
        "client services", "support specialist", "support associate",
        "implementation", "onboarding associate",
    )),
    "ops": (3, (
        "operations associate", "operations coordinator", "business operations",
        "office coordinator", "office manager", "administrative assistant",
        "administrative coordinator", "program coordinator", "project coordinator",
    )),
    "analyst": (4, (
        "data analyst", "business analyst", "reporting analyst", "operations analyst",
    )),
    "content": (4, (
        "content", "communications", "marketing coordinator", "marketing associate",
        "social media", "copywriter",
    )),
}

# Substring kill-list — seniority he can't reach, and technical ICs needing a coding
# interview. Mirrors the HARD FIT FILTER in CLAUDE.md; keep the two in sync.
# The "executive"/"sourcer"/contract entries came out of the 2026-07-19 wave, where
# 54 of 72 sourced jobs were rejected by the brain after opening them — Account
# Executive and Customer Success Executive are quota roles, recruiting sourcer roles
# want recruiting experience, and contract/temp postings aren't what he's after.
EXCLUDE = (
    "senior", "sr.", "sr ", "(sr", "sr)", "staff", "principal", "director", "vp ",
    "vice president", "head of", "manager", "lead ", " lead", "architect", "chief",
    "executive", "business partner", "commander", "sourcer",
    "contract", "temporary", "temp ", "part time", "part-time", "seasonal",
    " ii", " iii", " iv", "level 2", "level 3",
    "software engineer", "software developer", "developer", "engineer", "engineering",
    "data scientist", "machine learning", "devops", "backend", "back end", "frontend",
    "front end", "full stack", "full-stack", "firmware", "scientist", "researcher",
    "counsel", "attorney", "paralegal", "legal", "nurse", "clinical", "physician",
    "pharmac", "cpa", "tax ", "audit", "accountant", "intern", "phd",
    # Pipelines and evergreen reqs, not open roles — applying to these goes nowhere.
    "expression of interest", "talent pool", "talent community", "general application",
    "future opportunity", "join our",
    # Geographic restriction spelled out in the title rather than the location, e.g.
    # "Sales Representative (Central Midwest Candidates Only)" filed under "United States".
    "candidates only", "residents only", "must reside in", "must be located in",
    # Roles gated on an ALREADY-ACTIVE clearance. profile.yaml records none, and a
    # clearance takes months and a sponsoring employer — so these can't convert.
    # Delete these four lines if Zach does in fact hold one.
    "cleared", "clearance", "ts/sci", "polygraph",
    "tier 3",  # escalation tier — tier 1/2 support stays in-lane
)
# Signals a posting is calibrated for zero experience — these get ranked up.
EARLY_SIGNALS = (
    "new grad", "early career", "entry level", "entry-level", "associate", "trainee",
    "rotational", "apprentice", " i ", "university", "graduate",
)
# Non-US markers that disqualify an otherwise-"remote" posting.
NON_US = (
    "canada", "toronto", "vancouver", "montreal", "uk", "united kingdom", "london",
    "ireland", "dublin", "emea", "europe", "germany", "berlin", "munich", "france",
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
US_ONLY_LOCATIONS = frozenset({
    "united states", "united states of america", "us", "usa", "u s", "u s a",
    "us remote", "remote us", "remote united states", "united states remote",
    "nationwide", "anywhere in the us", "anywhere in the united states",
})


def _squash(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for exact location compares."""
    return " ".join("".join(c if c.isalnum() else " " for c in text).lower().split())


_STATE_IN_TITLE = re.compile(r",\s*([A-Z]{2})\b")


def _names_other_state(title: str) -> bool:
    """True if the title pins the role to a non-California state, e.g.
    "Outside Sales Representative - Portland, OR". Such postings are routinely filed
    under a country-wide location, so the location string alone won't catch them."""
    return any(s != "CA" for s in _STATE_IN_TITLE.findall(title))


# --- pool + rotation state ---------------------------------------------------
def load_pool() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in POOL_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        platform, token = line.split(":", 1)
        platform, token = platform.strip().lower(), token.strip()
        if platform in ("greenhouse", "lever", "ashby") and token:
            out.append((platform, token))
    return out


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
                }
    return state


def save_rotation(state: dict[tuple[str, str], dict]) -> None:
    ROTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ROTATION_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(ROTATION_HEADER)
        for (platform, token), v in sorted(state.items()):
            w.writerow([platform, token, v["last_mined"], v.get("mines", 0), v["hits"], v["fails"]])


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


def pick_boards(pool, state, k: int) -> list[tuple[str, str]]:
    """Weighted sample without replacement, weight = staleness × productivity.

    Efraimidis–Spirakis: key = random() ** (1/weight), take the top k. Stale boards
    win most of the time, but every live board keeps a real chance — that residual
    randomness is what makes two consecutive sessions see different companies.

    Productivity is the explore/exploit half: a board that has produced a matching job
    before is worth more than one mined three times for nothing (most big boards have
    no Bay-Area entry-level roles at all). Duds are demoted, never dropped — their
    postings turn over, and staleness eventually floats them back up.
    """
    live = [b for b in pool if state.get(b, {}).get("fails", 0) < DEAD_AFTER]
    keyed = []
    for b in live:
        s = state.get(b, {})
        weight = 1.0 + _age_hours(s.get("last_mined", ""))
        if s.get("hits", 0) > 0:
            weight *= 2.0                      # exploit: it has paid out before
        elif s.get("mines", 0) >= 3:
            weight *= 0.25                     # demote: mined repeatedly, never a match
        keyed.append((random.random() ** (1.0 / weight), b))
    keyed.sort(reverse=True)
    return [b for _, b in keyed[:k]]


# --- filters -----------------------------------------------------------------
def classify(title: str, lane_filter: str | None) -> str | None:
    """Return the lane a title belongs to, or None if it isn't a target role."""
    t = f" {title.lower()} "
    if any(x in t for x in EXCLUDE):
        return None
    for lane, (_tier, keywords) in LANES.items():
        if lane_filter and lane != lane_filter:
            continue
        if any(k in t for k in keywords):
            return lane
    return None


def location_ok(location: str, title: str, url: str = "") -> bool:
    """Bay Area, or a fully-remote-US role (Jack, 2026-07-19). Everything else drops."""
    blob = f"{location} {title}".lower()
    if any(x in blob for x in NON_US):
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
    if "remote" in blob and any(x in blob for x in US_REMOTE):
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


# --- board fetchers ----------------------------------------------------------
def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "applier/1.0"})
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
    ap.add_argument("--per-company", type=int, default=3,
                    help="max jobs from any one company (default 3)")
    ap.add_argument("--include-seen", action="store_true", help="don't dedup against seen/applied")
    ap.add_argument("--no-rotate", action="store_true", help="don't update rotation state")
    ap.add_argument("--stats", action="store_true", help="print rotation state and exit")
    args = ap.parse_args(argv[1:])

    wanted = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    pool = [b for b in load_pool() if b[0] in wanted]
    state = load_rotation()
    if not pool:
        print(f"no boards for platforms={sorted(wanted)}", file=sys.stderr)
        return 1

    if args.stats:
        live = [b for b in pool if state.get(b, {}).get("fails", 0) < DEAD_AFTER]
        never = [b for b in live if not state.get(b, {}).get("last_mined")]
        dead = [b for b in pool if state.get(b, {}).get("fails", 0) >= DEAD_AFTER]
        print(f"pool={len(pool)} live={len(live)} never_mined={len(never)} pruned={len(dead)}")
        ranked = sorted(live, key=lambda b: -_age_hours(state.get(b, {}).get("last_mined", "")))
        for b in ranked[:15]:
            age = _age_hours(state.get(b, {}).get("last_mined", ""))
            print(f"  {b[0]}:{b[1]}  stale={age/24:.1f}d  hits={state.get(b, {}).get('hits', 0)}")
        return 0

    boards = pick_boards(pool, state, args.boards)
    if not boards:
        print("no live boards in pool — check scripts/companies.txt", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(fetch, boards))

    known = set() if args.include_seen else load_known_urls()
    blocked = load_blocked_companies()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, ok_boards, dead_boards, throttled_boards = [], 0, 0, 0

    for board, postings, status in results:
        entry = state.setdefault(board, {"last_mined": "", "mines": 0, "hits": 0, "fails": 0})
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
            tier = LANES[lane][0]
            early = any(s in f" {title.lower()} " for s in EARLY_SIGNALS)
            rows.append({
                "sort": (tier - (0.5 if early else 0), random.random()),
                "url": url, "company": board[1], "title": title,
                "location": p["location"] or "?", "lane": lane,
            })
            entry["hits"] += 1

    # Cap per company before truncating — one 700-posting board would otherwise
    # eat the whole wave, which is the crowding-out this script exists to fix.
    rows.sort(key=lambda r: r["sort"])
    per_company: dict[str, int] = {}
    capped = []
    for r in rows:
        if per_company.get(r["company"], 0) >= args.per_company:
            continue
        per_company[r["company"]] = per_company.get(r["company"], 0) + 1
        capped.append(r)
    rows = capped[: args.n]

    if not args.no_rotate:
        save_rotation(state)

    for r in rows:
        print("\t".join([r["url"], r["company"], r["title"], r["location"], r["lane"]]))

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
