#!/usr/bin/env python3
"""search_jobs.py — find cyber jobs by SEARCHING for the title, not by draining a pool.

Why it exists (Jack, 2026-09-18): source.py samples boards out of a fixed pool and reads
whatever those boards happen to be posting that day. With nine lanes open that filled a
wave easily; with only `security` + `it` open it does not — most boards have no cyber
opening on any given day, so a pool-first wave reports the market exhausted while real
postings sit one search away. This inverts the order: start from the TITLE, find the
board, then keep the board.

    python3 scripts/search_jobs.py --queries        # the browser query matrix, one per line
    python3 scripts/search_jobs.py --ingest urls.txt  # harvested URLs -> TSV queue
    cat urls.txt | python3 scripts/search_jobs.py --ingest -
    python3 scripts/search_jobs.py --apis           # keyless remote-job APIs only

Three providers, in the order they earn their keep:

1. BROWSER SEARCH (primary, driven by the brain). `--queries` prints `site:`-scoped
   search queries; the brain runs them through the Playwright MCP browser it already has
   open and pipes the harvested links back into `--ingest`. It has to be the browser:
   plain HTTP to html.duckduckgo.com returns a result-less page and lite.duckduckgo.com
   answers 202 with a challenge (measured 2026-09-18), while the browser path is the one
   bot/job_finder.py proved.
2. KEYLESS APIs (`--apis`). RemoteOK and Remotive, no key, no browser. Remote-only, which
   is exactly the half of the mandate the Bay-Area filter can't reach.
3. Keyed APIs — Adzuna, USAJOBS — not wired: both need a free registration Jack has to do.

SLUG HARVEST is what actually fixes exhaustion. Every posting URL found yields a
`platform:slug`; the slug is appended to scripts/companies.txt and probed through
source.fetch() for ALL of that company's open in-lane roles. One search hit for one SOC
Analyst pulls that employer's whole board, and the pool becomes a cache of places search
has already found rather than the thing being consumed.

Every row still passes source.classify() / location_ok() / verify_rows() / the dedup and
per-company gates. The filters are not re-implemented here — search only changes what
gets offered to them.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import source as S  # noqa: E402  — single source of truth for every filter
from url_norm import normalize as _norm  # noqa: E402

# Titles to search for. These are query strings, NOT the keyword matcher — classify()
# still decides fit, so this list only has to be good at retrieval. Ordered roughly by
# how much of the Bay/remote cyber market each one reaches.
QUERY_TITLES = (
    "SOC Analyst",
    "Security Analyst",
    "Information Security Analyst",
    "GRC Analyst",
    "Security Compliance Analyst",
    "IT Audit Analyst",
    "SOX Analyst",
    "Risk Analyst",
    "Third Party Risk Analyst",
    "Compliance Analyst",
    "ISSO",
    "Security Controls Assessor",
    "RMF Analyst",
    "Cyber Risk Analyst",
    "Trust and Safety Analyst",
    "Cloud Security Analyst",
    "Vulnerability Management Analyst",
    "Identity and Access Management Analyst",
    "Insider Threat Analyst",
    "Security Operations Center Analyst",
    # The one feeder lane left.
    "IT Support Specialist",
    "Help Desk Technician",
)
# Only hosts we can actually submit on. Ashby is Tier 3 HARD-AVOID per CLAUDE.md SITE
# ROUTING (blocks at submit regardless of IP) and Workday/iCIMS/SmartRecruiters are in
# source.SUBMIT_BLOCKED, so searching them would fill a wave with dead ends.
SEARCH_SITES = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "breezy.hr",
)
# Matching location_ok(): Bay, NYC, Austin, Chicago (added 2026-10-24) and fully-remote-US.
# DC/NoVA stay out — Jack's call 2026-09-18, re-confirmed 2026-10-24, even though they
# hold most RMF/ISSO work.
SEARCH_LOCATIONS = ("San Francisco Bay Area", "New York City", "Austin TX", "Chicago IL",
                    "remote United States")


# Employers who reliably post this kind of work — probed on every run, never sampled.
EMPLOYERS_PATH = ROOT / "scripts" / "cyber_employers.txt"


def queries(sample: int = 0) -> list[str]:
    """The browser query matrix: title x location x submittable ATS host.

    The full matrix is 170+ queries, which is more than one wave's browser budget, so
    `sample` takes a random subset. Random, not the first N: a fixed prefix would search
    "SOC Analyst" every wave and never reach "Security Controls Assessor".
    """
    out = []
    for title in QUERY_TITLES:
        for loc in SEARCH_LOCATIONS:
            for site in SEARCH_SITES:
                out.append(f'"{title}" {loc} site:{site}')
    if sample and sample < len(out):
        return random.sample(out, sample)
    return out


def load_employers() -> set[tuple[str, str]]:
    if not EMPLOYERS_PATH.exists():
        return set()
    out = set()
    for line in EMPLOYERS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        platform, token = line.split(":", 1)
        platform, token = platform.strip().lower(), token.strip().lower()
        if platform in S.KNOWN_PLATFORMS and token:
            out.add((platform, token))
    return out


# --- slug extraction ---------------------------------------------------------
# board URL -> (platform, token). Only shapes source.fetch() can probe.
_SLUG_PATTERNS = (
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("breezy", re.compile(r"https?://([A-Za-z0-9_-]+)\.breezy\.hr/")),
    ("recruitee", re.compile(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com/")),
    ("teamtailor", re.compile(r"https?://([A-Za-z0-9_-]+)\.teamtailor\.com/")),
)


def slug_of(url: str) -> tuple[str, str] | None:
    for platform, pat in _SLUG_PATTERNS:
        m = pat.match(url.strip())
        if m and m.group(1):
            token = m.group(1).lower()
            if token not in {"www", "jobs", "app", "api", "embed"}:
                return (platform, token)
    return None


_CO_NOISE = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|corp|corporation|company|co|holdings|group|the|technologies"
    r"|technology|systems|solutions|services|software|labs|international|usa|us"
    r"|nyse|nasdaq|nasdaq gs|plc|gmbh|sa|ag)\b", re.I)


def slug_candidates(name: str) -> list[str]:
    """Plausible ATS tokens for a company NAME.

    Search results give a company name, not a board URL — HiringCafe's cards name the
    employer but hide the apply link behind a click, and LinkedIn is the same. Guessing
    the token and probing is cheaper than a click per card, and a wrong guess costs one
    404 that source.fetch() already handles.
    """
    base = re.sub(r"[^\w\s.-]", " ", name or "").strip()
    base = re.sub(r"\.(ai|io|com|co|net|org)\b", r"\1", base, flags=re.I)  # Horizon3.ai -> Horizon3ai
    stripped = _CO_NOISE.sub(" ", base)
    out: list[str] = []
    for variant in (stripped, base):
        words = [w for w in re.split(r"[\s._-]+", variant.lower()) if w]
        if not words:
            continue
        for token in ("".join(words), "-".join(words), words[0]):
            if len(token) >= 3 and token not in out:
                out.append(token)
    return out[:4]


def boards_for_names(names: list[str], platforms=("greenhouse", "lever", "breezy")
                     ) -> tuple[set[tuple[str, str]], int]:
    """Probe slug guesses for each company name; return the boards that answered."""
    from concurrent.futures import ThreadPoolExecutor
    guesses: list[tuple[str, str]] = []
    have = set(S.load_pool()) | load_employers()
    for name in names:
        for token in slug_candidates(name):
            for platform in platforms:
                b = (platform, token)
                if b not in have and b not in guesses:
                    guesses.append(b)
    found: set[tuple[str, str]] = set()
    if guesses:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for board, postings, status in ex.map(S.fetch, guesses):
                if status == "ok" and postings is not None:
                    found.add(board)
    print(f"[search] {len(names)} company names -> {len(guesses)} slug guesses -> "
          f"{len(found)} live boards", file=sys.stderr)
    # Names whose board was already known are not re-probed but still worth serving.
    known_boards = {b for name in names for t in slug_candidates(name)
                    for b in ((p, t) for p in platforms) if b in have}
    return found | known_boards, len(guesses)


def add_to_pool(slugs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Append boards search found to companies.txt. Returns the ones that were new.

    Appending only — the pool is now a cache of what search has turned up, so nothing
    here prunes it. source.save_rotation() has its own hard-won rules about pruning.
    """
    have = set(S.load_pool())
    new = {b for b in slugs if b not in have}
    if new:
        with S.POOL_PATH.open("a") as f:
            for platform, token in sorted(new):
                f.write(f"{platform}:{token}\n")
    return new


# --- keyless APIs ------------------------------------------------------------
_UA = "Mozilla/5.0 (X11; Linux x86_64) auto-applier/1.0"


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _remote_us(raw: str) -> str:
    """Normalise an aggregator's remote-scope string into something location_ok reads.

    A worldwide-remote posting includes the US by definition, so it is reported as
    remote-US; anything that names only other countries is left alone and location_ok
    drops it.
    """
    low = (raw or "").lower()
    if not low.strip() or "worldwide" in low or "anywhere" in low:
        return "Remote - US"
    if "usa" in low or "united states" in low or re.search(r"\bus\b", low):
        return "Remote - US"
    return raw


def remoteok_jobs() -> list[dict]:
    """https://remoteok.com/api — first element is their legal/attribution notice."""
    data = _get_json("https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        out.append({
            "title": j.get("position", ""),
            "company": j.get("company", "") or "?",
            "location": _remote_us(j.get("location", "")),
            "url": j.get("url", "") or j.get("apply_url", ""),
        })
    return out


def remotive_jobs(titles=QUERY_TITLES) -> list[dict]:
    """https://remotive.com/api/remote-jobs?search= — one call per title, keyword search."""
    out = []
    for title in titles:
        q = urllib.parse.quote(title)
        try:
            data = _get_json(f"https://remotive.com/api/remote-jobs?search={q}&limit=50")
        except Exception as e:  # noqa: BLE001 — one dead provider must not kill the wave
            print(f"[search] remotive {title!r}: {e}", file=sys.stderr)
            continue
        for j in data.get("jobs", []):
            out.append({
                "title": j.get("title", ""),
                "company": j.get("company_name", "") or "?",
                "location": _remote_us(j.get("candidate_required_location", "")),
                "url": j.get("url", ""),
            })
    return out


def api_jobs() -> list[dict]:
    jobs: list[dict] = []
    for name, fn in (("remoteok", remoteok_jobs), ("remotive", remotive_jobs)):
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001
            print(f"[search] {name} unavailable: {e}", file=sys.stderr)
            continue
        print(f"[search] {name}: {len(got)} postings", file=sys.stderr)
        jobs += got
    return jobs


# --- assembling rows ---------------------------------------------------------
def _row(url: str, company: str, title: str, location: str, lane: str,
         board: tuple[str, str] | None) -> dict:
    """Same shape and sort key source.py's mining path produces."""
    early = any(s in f" {title.lower()} " for s in S.EARLY_SIGNALS)
    return {
        "sort": (S._tier(lane) - (0.5 if early else 0), random.random()),
        "url": url, "company": company, "title": title,
        "location": location or "?", "lane": lane, "board": board,
    }


def keep(url: str, title: str, location: str, lane_filter: str | None,
         known: set[str], blocked: set[str], company: str) -> str | None:
    """The shared gate. Returns the lane, or None with the reason counted by the caller."""
    if not url or _norm(url) in known:
        return None
    if S.platform_of(url) in S.SUBMIT_BLOCKED:
        return None
    if company.lower() in blocked:
        return None
    lane = S.classify(title, lane_filter)
    if not lane or not S.location_ok(location, title, url):
        return None
    return lane


def probe_boards(boards: set[tuple[str, str]], lane_filter, known, blocked) -> list[dict]:
    """Probe each board search turned up for ALL of its open in-lane postings."""
    from concurrent.futures import ThreadPoolExecutor
    rows: list[dict] = []
    if not boards:
        return rows
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(S.fetch, sorted(boards)))
    live = 0
    for board, postings, status in results:
        if status != "ok" or not postings:
            continue
        live += 1
        for p in postings:
            lane = keep(p["url"], p["title"], p["location"], lane_filter,
                        known, blocked, board[1])
            if not lane:
                continue
            known.add(_norm(p["url"]))
            rows.append(_row(p["url"], board[1], p["title"], p["location"], lane, board))
    print(f"[search] probed {len(boards)} boards, {live} answered, "
          f"{len(rows)} in-lane postings", file=sys.stderr)
    return rows


def search_rows(urls: list[str] | None = None, lane_filter: str | None = None,
                use_apis: bool = True, probe: bool = True, employers: bool = True,
                names: list[str] | None = None, include_seen: bool = False) -> list[dict]:
    """Rows from the employer list + harvested search hits + the keyless APIs."""
    known = set() if include_seen else S.load_known_urls()
    blocked = S.load_blocked_companies()
    rows: list[dict] = []

    if employers:
        rows += probe_boards(load_employers(), lane_filter, known, blocked)

    slugs: set[tuple[str, str]] = set()
    for url in urls or []:
        url = url.strip()
        if not url.lower().startswith("http"):
            continue
        b = slug_of(url)
        if b:
            slugs.add(b)
    if names:
        found, _ = boards_for_names([n for n in names if n.strip()])
        slugs |= found
    if slugs:
        new = add_to_pool(slugs)
        print(f"[search] {len(slugs)} boards harvested, {len(new)} new to the pool",
              file=sys.stderr)
        if probe:
            rows += probe_boards(slugs, lane_filter, known, blocked)

    if use_apis:
        n_api = 0
        for j in api_jobs():
            lane = keep(j["url"], j["title"], j["location"], lane_filter,
                        known, blocked, j["company"])
            if not lane:
                continue
            known.add(_norm(j["url"]))
            rows.append(_row(j["url"], j["company"], j["title"], j["location"], lane, None))
            n_api += 1
        print(f"[search] {n_api} in-lane postings from the keyless APIs", file=sys.stderr)

    rows.sort(key=lambda r: r["sort"])
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Title-first job search (see module docstring).")
    ap.add_argument("--queries", action="store_true",
                    help="print the browser query matrix and exit")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="with --queries, print a random N of the matrix (a wave's budget)")
    ap.add_argument("--no-employers", action="store_true",
                    help="skip scripts/cyber_employers.txt, the always-probed board list")
    ap.add_argument("--ingest", metavar="FILE",
                    help="file of harvested posting URLs, one per line ('-' for stdin)")
    ap.add_argument("--names", metavar="FILE",
                    help="file of EMPLOYER NAMES harvested from a job-search site, one per "
                         "line ('-' for stdin). Their ATS token is guessed and probed — "
                         "this is the cheap path when the results page shows the company "
                         "but hides the apply link (HiringCafe, LinkedIn)")
    ap.add_argument("--apis", action="store_true",
                    help="run only the keyless remote-job APIs (no harvested URLs)")
    ap.add_argument("--no-apis", action="store_true", help="skip the keyless APIs")
    ap.add_argument("--no-probe", action="store_true",
                    help="don't probe the boards found for their other open roles")
    ap.add_argument("--n", type=int, default=30, help="max jobs to print (default 30)")
    ap.add_argument("--lane", choices=sorted(S.LANES), help="restrict to one lane")
    ap.add_argument("--max-years", type=int, default=S.DEFAULT_MAX_YEARS)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the ATS single-job verify pass")
    ap.add_argument("--per-company", type=int, default=4)
    ap.add_argument("--include-seen", action="store_true", help="don't dedup against seen/applied")
    args = ap.parse_args(argv[1:])

    if args.queries:
        qs = queries(args.sample)
        for q in qs:
            print(q)
        print(f"[search] {len(qs)} queries — run them in the browser and pipe the "
              f"posting URLs back through --ingest", file=sys.stderr)
        return 0

    def _lines(path: str) -> list[str]:
        src = sys.stdin if path == "-" else open(path)
        with src:
            return [ln.strip() for ln in src.read().splitlines() if ln.strip()]

    urls = _lines(args.ingest) if args.ingest else []
    names = _lines(args.names) if args.names else []

    rows = search_rows(urls, lane_filter=args.lane, use_apis=not args.no_apis,
                       probe=not args.no_probe, employers=not args.no_employers,
                       names=names, include_seen=args.include_seen)

    # Same lifetime employer ceiling the mining path applies.
    per_company = S.prior_applications({r["company"] for r in rows})
    capped = []
    for r in rows:
        if per_company.get(r["company"], 0) >= args.per_company:
            continue
        per_company[r["company"]] = per_company.get(r["company"], 0) + 1
        capped.append(r)

    served = S.verify_rows(capped, args.n, args.max_years,
                           enabled=not args.no_verify, label=" (search)")
    for r in served:
        print("\t".join([r["url"], r["company"], r["title"], r["location"], r["lane"]]))
    lanes: dict[str, int] = {}
    for r in served:
        lanes[r["lane"]] = lanes.get(r["lane"], 0) + 1
    print(f"[search] {len(served)} jobs served from {len(rows)} found "
          f"({dict(sorted(lanes.items()))})", file=sys.stderr)
    return 0 if served else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
