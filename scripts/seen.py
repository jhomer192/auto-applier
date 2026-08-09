#!/usr/bin/env python3
"""seen.py — track URLs the bot has surfaced/considered but not yet applied to.

Use case: bot runs a search, finds 50 listings, surfaces 5 to the user. The 45
others shouldn't be re-investigated next scan unless something changed. This
script keeps the "we've seen this URL" set out of the bot's context.

    python scripts/seen.py check <url>
    python scripts/seen.py mark  <url> <company> <title> [location]
    python scripts/seen.py count
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from url_norm import normalize as _normalize_url  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "seen.csv"
# `location` was added 2026-08-09. Rows written before that carry none, and source.py
# --backlog treats a missing one as "unknown" and lets verify_rows() fetch the real
# location from the ATS instead. Appending the column rather than rewriting history
# keeps every existing row readable by csv.DictReader.
HEADER = ["first_seen", "url", "company", "title", "location"]


def _ensure_csv() -> None:
    """Create the file, and widen a pre-`location` header in place if it is still there.

    Without this the 4-column header stays and every new 5-field row spills its location
    into DictReader's restkey, where nothing can read it. Only the header line changes —
    the 4,300 existing rows are left exactly as they are and simply read back with an
    empty location, which is what source.py --backlog already expects.
    """
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)
        return
    with CSV_PATH.open(newline="") as f:
        rows = list(csv.reader(f))
    if rows and rows[0] != HEADER and rows[0][:4] == HEADER[:4]:
        rows[0] = HEADER
        tmp = CSV_PATH.with_suffix(".csv.tmp")
        with tmp.open("w", newline="") as f:
            csv.writer(f).writerows(rows)
        tmp.replace(CSV_PATH)


def cmd_check(query: str) -> int:
    if not query.strip():
        print("NONE")
        return 0
    norm = _normalize_url(query)
    if not CSV_PATH.exists():
        print("NONE")
        return 0
    with CSV_PATH.open(newline="") as f:
        for row in csv.DictReader(f):
            if _normalize_url(row.get("url", "")) == norm:
                print(f"MATCH: {row.get('first_seen', '')}  {row.get('company', '')}  {row.get('title', '')}")
                return 0
    print("NONE")
    return 0


def cmd_mark(args: list[str]) -> int:
    if len(args) < 1:
        print("usage: seen.py mark <url> [company] [title] [location]")
        return 2
    url = args[0]
    company = args[1] if len(args) > 1 else ""
    title = args[2] if len(args) > 2 else ""
    location = args[3] if len(args) > 3 else ""
    _ensure_csv()
    # dedup on normalized URL
    norm = _normalize_url(url)
    with CSV_PATH.open(newline="") as f:
        for row in csv.DictReader(f):
            if _normalize_url(row.get("url", "")) == norm:
                print("already seen")
                return 0
    with CSV_PATH.open("a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            url, company, title, location,
        ])
    print("OK marked")
    return 0


def cmd_count(_args: list[str]) -> int:
    if not CSV_PATH.exists():
        print(0)
        return 0
    with CSV_PATH.open(newline="") as f:
        # subtract header
        print(sum(1 for _ in f) - 1)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: seen.py {check|mark|count} ...")
        return 2
    sub, rest = argv[1], argv[2:]
    if sub == "check":
        return cmd_check(rest[0] if rest else "")
    if sub == "mark":
        return cmd_mark(rest)
    if sub == "count":
        return cmd_count(rest)
    print(f"unknown subcommand: {sub}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
