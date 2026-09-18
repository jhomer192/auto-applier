#!/usr/bin/env python3
"""applied.py — context-light dedup + record helper for the applier bot.

Instead of the bot Reading applied.csv into its context (which grows
unbounded), the bot calls this script:

    python scripts/applied.py check  <url-or-company>
    python scripts/applied.py record <url> <company> <title> [platform] [lane]
    python scripts/applied.py recent [N]
    python scripts/applied.py count

`check` does:
  - exact URL match (fastest)
  - normalized URL match (strips query strings + tracking params + trailing slash)
  - canonical company match (strips suffixes, collapses whitespace, whole-word substring)

If anything matches, it prints `MATCH:` followed by the matching rows (≤5).
If nothing matches, it prints `NONE`. Bot reads only the result, not the file.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from company_match import matches as _company_matches  # noqa: E402
from url_norm import normalize as _normalize_url  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "applied.csv"
TAILOR_LOG = Path(__file__).resolve().parent.parent / "data" / "tailor_log.csv"
RESUME = Path(__file__).resolve().parent.parent / "data" / "resume_tailored.pdf"
EVENTS = Path(__file__).resolve().parent.parent / "data" / "apply_events.jsonl"
HEADER = ["date_applied", "url", "company", "title", "platform", "status", "notes"]


def _lane_sent() -> str:
    """Which resume lane this application carried, read off the resume actually uploaded.

    The lane mix is the number the 2026-09-18 cyber pivot has to move (it was sdr 370 /
    ops 225 / security 112 across 707 tailored resumes), and it was only recoverable by
    joining tailor_log.csv after the fact because `notes` was empty on all 1,227 rows.
    Derived rather than passed as a flag: a flag the brain has to remember is a flag that
    eventually goes unpassed. resume_tailored.pdf is the file it uploads unconditionally,
    so hashing it here records the lane that was really sent.
    """
    try:
        digest = hashlib.md5(RESUME.read_bytes()).hexdigest()
    except OSError:
        return ""
    try:
        with TAILOR_LOG.open(encoding="utf-8", newline="") as f:
            lanes = {r["md5"]: r.get("lane", "") for r in csv.DictReader(f) if r.get("md5")}
    except (OSError, KeyError, csv.Error):
        return ""
    return lanes.get(digest, "")


def _ensure_csv() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


@dataclass
class Row:
    date_applied: str
    url: str
    company: str
    title: str
    platform: str
    status: str
    notes: str

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "Row":
        return cls(**{k: d.get(k, "") for k in HEADER})


def _iter_rows() -> Iterable[Row]:
    if not CSV_PATH.exists():
        return
    with CSV_PATH.open(newline="") as f:
        for d in csv.DictReader(f):
            yield Row.from_dict(d)


def _prior_judgment(norm_url: str | None) -> str:
    """A fit-skip this wave has already made, for a URL applied.csv cannot know about.

    applied.csv records wins, so a posting skipped for fit leaves no mark there and can be
    re-queued from the backlog hours later and judged the other way. That happened on the
    first live cyber wave: a Roblox req was skipped at 21:47 for "4+ years, CL9 senior",
    then applied to at 22:47. The attempt log is the only place that decision survives, so
    check is where it has to surface.
    """
    if not norm_url or not EVENTS.exists():
        return ""
    hit = None
    for line in EVENTS.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("outcome") not in ("skipped", "blocked"):
            continue
        url = e.get("url") or ""
        if url and _normalize_url(url) == norm_url:
            hit = e   # last word wins
    if not hit:
        return ""
    return (f"PRIOR {hit['outcome']}: {hit.get('ts', '')}  {hit.get('company', '')}  "
            f"{hit.get('title', '')}  reason: {hit.get('block_reason') or 'none recorded'}\n"
            "  You already judged this posting. Apply anyway ONLY if that reason is wrong; "
            "otherwise move on.")


def cmd_check(query: str) -> int:
    q = query.strip()
    if not q:
        print("NONE")
        return 0
    is_url = q.startswith("http://") or q.startswith("https://")
    norm_q = _normalize_url(q) if is_url else None
    matches: list[Row] = []
    for row in _iter_rows():
        if is_url:
            if row.url.strip().lower() == q.lower():
                matches.append(row)
                continue
            if norm_q and _normalize_url(row.url) == norm_q:
                matches.append(row)
                continue
        if not is_url and row.company:
            if _company_matches(row.company, q):
                matches.append(row)
                continue
    if not matches:
        prior = _prior_judgment(norm_q) if is_url else ""
        print(prior or "NONE")
        return 0
    print(f"MATCH: {len(matches)}")
    for r in matches[:5]:
        print(f"  {r.date_applied}  {r.company}  {r.title}  {r.url}  ({r.status})")
    return 0


def cmd_record(args: list[str]) -> int:
    if len(args) < 3:
        print("usage: applied.py record <url> <company> <title> [platform] [lane]")
        return 2
    url, company, title = args[0], args[1], args[2]
    platform = args[3] if len(args) > 3 else ""
    lane = args[4] if len(args) > 4 else _lane_sent()
    _ensure_csv()
    row = [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        url,
        company,
        title,
        platform,
        "applied",
        f"lane={lane}" if lane else "",
    ]
    with CSV_PATH.open("a", newline="") as f:
        csv.writer(f).writerow(row)
    print(f"OK recorded: {company} — {title}" + (f" [lane={lane}]" if lane else ""))
    return 0


def cmd_recent(args: list[str]) -> int:
    n = int(args[0]) if args and args[0].isdigit() else 10
    rows = list(_iter_rows())
    for r in rows[-n:]:
        print(f"{r.date_applied}  {r.company}  {r.title}  {r.url}")
    if not rows:
        print("(none yet)")
    return 0


def cmd_count(_args: list[str]) -> int:
    print(sum(1 for _ in _iter_rows()))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: applied.py {check|record|recent|count} ...")
        return 2
    sub, rest = argv[1], argv[2:]
    if sub == "check":
        return cmd_check(rest[0] if rest else "")
    if sub == "record":
        return cmd_record(rest)
    if sub == "recent":
        return cmd_recent(rest)
    if sub == "count":
        return cmd_count(rest)
    print(f"unknown subcommand: {sub}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
