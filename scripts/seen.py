#!/usr/bin/env python3
"""seen.py — track URLs the bot has surfaced/considered but not yet applied to.

Use case: bot runs a search, finds 50 listings, surfaces 5 to the user. The 45
others shouldn't be re-investigated next scan unless something changed. This
script keeps the "we've seen this URL" set out of the bot's context.

    python scripts/seen.py check <url>
    python scripts/seen.py mark  <url> <company> <title>
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
HEADER = ["first_seen", "url", "company", "title"]


def _ensure_csv() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


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
        print("usage: seen.py mark <url> [company] [title]")
        return 2
    url = args[0]
    company = args[1] if len(args) > 1 else ""
    title = args[2] if len(args) > 2 else ""
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
            url, company, title,
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
