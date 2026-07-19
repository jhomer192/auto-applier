#!/usr/bin/env python3
"""source.py — rotating job sourcer. Run this at the START of every apply wave.

Why it exists: web search returns the same (often stale) postings every session, and
the brain re-mines its favourite five companies, so waves run dry on jobs already in
applied.csv/seen.csv. This queries live ATS board APIs across a large pool of boards
and *rotates* which ones it hits, so consecutive sessions see different companies.

    python3 scripts/source.py                    # 30 fresh jobs from ~45 rotated boards
    python3 scripts/source.py --n 60 --boards 90 # deeper backlog for a long wave
    python3 scripts/source.py --lane security    # only one lane's keywords
    python3 scripts/source.py --stats            # rotation state, no fetching

Output is TSV — url, company, title, location, lane — one job per line, already
deduped against applied.csv + seen.csv and already location/seniority filtered. Fit
decisions and the apply itself are still yours; this only decides what to look at.

Rotation: data/board_rotation.csv holds last_mined/hits/fails per board. Boards are
sampled weighted by staleness (Efraimidis–Spirakis), so the longest-unmined boards
are the likeliest picks but every board keeps a real chance — the randomness is what
stops each session converging on the same pool. Boards that 404 four times running
are pruned automatically.

Pool lives in scripts/companies.txt (`platform:token`). Add companies there.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
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
BAY_TERMS = (
    "bay area", "sf bay", "san francisco bay", "silicon valley", "the peninsula",
    "north bay", "east bay", "south bay",
    "san francisco", "sf,", "s.f.", "daly city", "brisbane", "south san francisco",
    "san mateo", "redwood city", "palo alto", "menlo park", "foster city",
    "burlingame", "san bruno", "millbrae", "san carlos", "belmont", "half moon bay",
    "east palo alto",
    "san jose", "santa clara", "sunnyvale", "mountain view", "cupertino", "milpitas",
    "los gatos", "campbell", "saratoga", "morgan hill", "gilroy",
    "oakland", "berkeley", "emeryville", "alameda", "fremont", "hayward", "richmond",
    "walnut creek", "pleasanton", "dublin, ca", "san ramon", "concord", "union city",
    "newark, ca", "castro valley", "san leandro", "pleasant hill", "danville",
    "livermore", "martinez", "lafayette", "orinda", "albany",
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
ROTATION_HEADER = ["platform", "token", "last_mined", "hits", "fails"]
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
        "onboarding specialist", "sourcer",
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
EXCLUDE = (
    "senior", "sr.", "sr ", "(sr", "sr)", "staff", "principal", "director", "vp ",
    "vice president", "head of", "manager", "lead ", " lead", "architect", "chief",
    "executive director", "executive assistant", "business partner", "commander",
    " ii", " iii", " iv", "level 2", "level 3",
    "software engineer", "software developer", "developer", "engineer", "engineering",
    "data scientist", "machine learning", "devops", "backend", "back end", "frontend",
    "front end", "full stack", "full-stack", "firmware", "scientist", "researcher",
    "counsel", "attorney", "paralegal", "legal", "nurse", "clinical", "physician",
    "pharmac", "cpa", "tax ", "audit", "accountant", "intern", "phd",
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
)
US_REMOTE = (
    "united states", "usa", "u.s.", "us-remote", "remote - us", "remote, us",
    "remote (us", "us remote", "anywhere in the us", "nationwide", "remote us",
)


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
            w.writerow([platform, token, v["last_mined"], v["hits"], v["fails"]])


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
    """Weighted sample without replacement, weight = staleness.

    Efraimidis–Spirakis: key = random() ** (1/weight), take the top k. Stale boards
    win most of the time, but every live board keeps a real chance — that residual
    randomness is what makes two consecutive sessions see different companies.
    """
    live = [b for b in pool if state.get(b, {}).get("fails", 0) < DEAD_AFTER]
    keyed = []
    for b in live:
        weight = 1.0 + _age_hours(state.get(b, {}).get("last_mined", ""))
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


def location_ok(location: str, title: str) -> bool:
    """Bay Area, or a fully-remote-US role (Jack, 2026-07-19). Everything else drops."""
    blob = f"{location} {title}".lower()
    if any(x in blob for x in NON_US):
        return False
    if is_bay_area(location, title):
        return True
    if "remote" in blob and any(x in blob for x in US_REMOTE):
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


def fetch(board: tuple[str, str]) -> tuple[tuple[str, str], list[dict] | None]:
    """Return (board, postings) — postings is None when the board failed to fetch."""
    platform, token = board
    try:
        if platform == "greenhouse":
            jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs").get("jobs", [])
            return board, [{
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
            } for j in jobs]
        if platform == "lever":
            jobs = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
            return board, [{
                "title": j.get("text", ""),
                "location": (j.get("categories") or {}).get("location", ""),
                "url": j.get("hostedUrl") or j.get("applyUrl") or "",
            } for j in jobs]
        jobs = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").get("jobs", [])
        return board, [{
            "title": j.get("title", ""),
            "location": str(j.get("location") or ""),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
        } for j in jobs]
    except Exception:  # noqa: BLE001 — 404s/timeouts are routine; the board just misses this wave
        return board, None


# --- main --------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Rotating live-board job sourcer.")
    ap.add_argument("--n", type=int, default=30, help="max jobs to print (default 30)")
    ap.add_argument("--boards", type=int, default=45, help="boards to mine this run (default 45)")
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
    rows, ok_boards, dead_boards = [], 0, 0

    for board, postings in results:
        entry = state.setdefault(board, {"last_mined": "", "hits": 0, "fails": 0})
        if postings is None:
            entry["fails"] += 1
            dead_boards += 1
            continue
        entry["fails"] = 0
        entry["last_mined"] = now
        ok_boards += 1
        if board[1].lower() in blocked:
            continue
        for p in postings:
            url, title = p["url"], p["title"]
            if not url or _norm(url) in known:
                continue
            lane = classify(title, args.lane)
            if not lane or not location_ok(p["location"], title):
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
    print(
        f"\n[source] {len(rows)} fresh jobs from {ok_boards} boards "
        f"({dead_boards} unreachable) — lanes: {lanes_seen}. "
        f"Rotation updated; next run mines different boards.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
