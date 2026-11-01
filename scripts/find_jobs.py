#!/usr/bin/env python3
"""find_jobs.py — market-wide harvest of submittable, in-reach jobs.

The pool mines boards someone already found; a full sweep of all of them on 2026-10-24
surfaced ~50 servable jobs. LinkedIn's logged-out search shows the whole market (10,830
cyber/IT cards across the metros that day) but hides each posting's apply link, and
slug guessing turned 425 hiring employers into only 20 boards. This closes that gap:

    LinkedIn cards -> hiring employers -> company domain (Clearbit autocomplete, keyless)
    -> careers page -> Greenhouse/Breezy board -> every in-lane posting on that board

and then runs the same gates the waves use. Output is a JSON list of candidates with the
posting text, for the fit judge; `--mark FILE` appends a judged TSV to seen.csv.

    python3 scripts/find_jobs.py --out data/find_jobs.json        # harvest (slow: paced)
    python3 scripts/find_jobs.py --mark data/top100.tsv           # url,company,title,location
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import search_jobs as J  # noqa: E402
import source as S  # noqa: E402
from url_norm import normalize as _norm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "find_jobs_cache.json"   # cards + resolved boards, so reruns are cheap

# Executive titles only; senior ICs and managers are in (Jack, 2026-11-01).
SENIOR = re.compile(r"\b(director|head|vp|chief|architect)\b", re.I)
# Needs a clearance he does not hold yet. His TS is in progress, so "ability to obtain"
# and "eligible for" stay in; only a present-tense requirement drops.
_ACTIVE = re.compile(r"\b(active|current)\s+(ts|top secret|ts/sci|secret)\b", re.I)
_HIGH = re.compile(r"\b(ts/sci|top secret/sci|polygraph)\b", re.I)
_FUTURE = re.compile(r"obtain|eligib|willing|able to|ability to", re.I)


def needs_active_clearance(text: str) -> bool:
    if _ACTIVE.search(text):
        return True
    return any(not _FUTURE.search(text[max(0, m.start() - 60):m.end() + 60])
               for m in _HIGH.finditer(text))


# Board references a careers page can carry. Ashby/Lever are recorded only to count them.
_BOARD_RE = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=|embed/job_app\?for=)?([A-Za-z0-9_-]+)")),
    ("breezy", re.compile(r"https?://([A-Za-z0-9_-]+)\.breezy\.hr")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
)
_NOT_SLUG = {"www", "jobs", "app", "api", "embed", "v1", "js", "boards", "job_board"}
CAREER_PATHS = ("", "/careers", "/jobs", "/careers/", "/company/careers", "/about/careers",
                "/join-us", "/about-us/careers")
_UA = J._LI_UA
# Titles searched only for this harvest, on top of the crawl's QUERY_TITLES. Each one
# passes S.classify, so its cards survive the lane gate below.
EXTRA_TITLES = (
    "Cybersecurity Analyst", "Junior Security Analyst", "Cybersecurity Specialist",
    "Information Security Specialist", "Incident Response Analyst",
    "Threat Intelligence Analyst", "Privacy Analyst", "Cyber Security Engineer I",
    "IT Specialist", "Desktop Support Technician", "Service Desk Analyst",
    "Technical Support Specialist", "IT Support Engineer", "Internal Audit Associate",
)


def _get_text(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def domain_of(company: str) -> str | None:
    """Company name -> its web domain via Clearbit's keyless autocomplete."""
    q = urllib.parse.quote(company)
    try:
        hits = json.loads(_get_text(f"https://autocomplete.clearbit.com/v1/companies/suggest?query={q}"))
    except Exception:  # noqa: BLE001
        return None
    want = re.sub(r"[^a-z0-9]", "", company.lower())
    for h in hits:
        if re.sub(r"[^a-z0-9]", "", (h.get("name") or "").lower()) == want:
            return h.get("domain")
    return hits[0].get("domain") if hits else None


def boards_on_site(domain: str) -> set[tuple[str, str]]:
    """Every ATS board a company's own site links to, from its home and careers pages."""
    found: set[tuple[str, str]] = set()
    for path in CAREER_PATHS:
        try:
            html = _get_text(f"https://{domain}{path}")
        except Exception:  # noqa: BLE001 — 404s and TLS errors are the common case
            continue
        for platform, pat in _BOARD_RE:
            for tok in pat.findall(html):
                tok = tok.lower()
                if tok not in _NOT_SLUG and len(tok) >= 2:
                    found.add((platform, tok))
        if any(p in ("greenhouse", "breezy") for p, _ in found):
            break
    return found


def harvest_employers(pages: int, cache: dict) -> dict[str, list[str]]:
    """LinkedIn employers with an in-lane, in-metro card -> the card titles seen."""
    emp: dict[str, list[str]] = cache.setdefault("employers", {})
    done: list[str] = cache.setdefault("searched", [])
    for title in J.QUERY_TITLES + EXTRA_TITLES:
        for geo, remote in J.LI_LOCATIONS:
            for level in ("", "2,3"):           # all levels, then entry+associate only
                key = f"{title}|{geo}|{level}"
                if key in done:
                    continue
                for t, company, loc in J.linkedin_cards(title, geo, remote, pages, level):
                    where = f"{loc} remote" if remote else loc
                    if S.classify(t, None) and S.location_ok(where, t):
                        emp.setdefault(company.strip(), []).append(t)
                done.append(key)
                _save(cache)
        print(f"[find] {title!r}: {len(emp)} employers so far", file=sys.stderr)
    return emp


def resolve(company: str) -> dict:
    """{'boards': [[platform, token], ...], 'domain': str|None, 'other': [...]}"""
    boards, _ = J.boards_for_names([company])
    out = {"boards": sorted(boards), "domain": None, "other": []}
    if boards:
        return out
    dom = domain_of(company)
    out["domain"] = dom
    if dom:
        site = boards_on_site(dom)
        live = []
        for b in sorted(site):
            if b[0] in ("greenhouse", "breezy"):
                _, postings, status = S.fetch(b)
                if status == "ok" and postings is not None:
                    live.append(list(b))
            else:
                out["other"].append(list(b))
        out["boards"] = live
    return out


def _save(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache))


def _posting_text(url: str) -> str:
    """Body text of a Greenhouse posting for the judge; empty for anything else."""
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if not m:
        return ""
    try:
        j = json.loads(_get_text(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}"))
    except Exception:  # noqa: BLE001
        return ""
    return S._strip_html(j.get("content", ""))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write candidate JSON here")
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--max-years", type=int, default=0)
    ap.add_argument("--mark", metavar="TSV", help="append url,company,title,location rows to seen.csv")
    args = ap.parse_args(argv[1:])

    if args.mark:
        rows = []
        for line in Path(args.mark).read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[0].startswith("http"):
                rows.append(dict(zip(("url", "company", "title", "location"), parts)))
        J.mark_seen(rows)
        return 0

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    emp = harvest_employers(args.pages, cache)

    resolved: dict = cache.setdefault("resolved", {})
    todo = [c for c in emp if c not in resolved]
    print(f"[find] resolving {len(todo)} employers ({len(resolved)} cached)", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (c, r) in enumerate(zip(todo, ex.map(resolve, todo)), 1):
            resolved[c] = r
            if i % 25 == 0:
                _save(cache)
                print(f"[find]   {i}/{len(todo)} resolved", file=sys.stderr)
    _save(cache)

    boards = {tuple(b) for r in resolved.values() for b in r["boards"]}
    n_other = sum(1 for r in resolved.values() if not r["boards"] and r["other"])
    print(f"[find] {len(emp)} employers -> {len(boards)} submittable boards "
          f"({n_other} more only on Lever/Ashby)", file=sys.stderr)
    J.add_to_pool(boards)

    known = S.load_known_urls()
    judged = S.attempted_titles()
    blocked = S.load_blocked_companies()
    rows = []
    for board in sorted(boards):
        _, postings, status = S.fetch(board)
        if status != "ok" or not postings:
            continue
        for p in postings:
            if SENIOR.search(p["title"]):
                continue
            if (board[1].lower(), p["title"].strip().lower()) in judged:
                continue
            lane = J.keep(p["url"], p["title"], p["location"], None, known, blocked, board[1])
            if lane:
                known.add(_norm(p["url"]))
                rows.append(J._row(p["url"], board[1], p["title"], p["location"], lane, board))
    print(f"[find] {len(rows)} in-lane, in-metro, never-attempted postings", file=sys.stderr)

    per = S.prior_applications({r["company"] for r in rows})
    rows = [r for r in rows if per.get(r["company"], 0) < 4]
    live = S.verify_rows(rows, len(rows), args.max_years, label=" (find)")

    out = []
    for r in live:
        text = _posting_text(r["url"])
        if text and needs_active_clearance(text):
            continue
        time.sleep(0.2 + random.random() * 0.2)
        out.append({k: r[k] for k in ("url", "company", "title", "location", "lane")}
                   | {"text": text[:4000]})
    print(f"[find] {len(out)} candidates for the fit judge", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
