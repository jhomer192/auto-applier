#!/usr/bin/env python3
"""skipped.py — context-light skip-company check + add helper.

    python scripts/skipped.py check <company>
    python scripts/skipped.py add   <company> [reason]
    python scripts/skipped.py list

Same idea as applied.py: bot calls this instead of Reading the file. Fuzzy
canonical match (strips suffixes, collapses whitespace, whole-word substring)
so "Acme AI Inc" matches "Acme AI", "Open AI" matches "OpenAI", etc.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from company_match import matches as _company_matches  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "skipped_companies.csv"
HEADER = ["company", "reason"]


def _ensure_csv() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


def _rows() -> list[dict[str, str]]:
    _ensure_csv()
    with CSV_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def cmd_check(query: str) -> int:
    q = query.strip()
    if not q:
        print("NONE")
        return 0
    hits = [r for r in _rows() if _company_matches(r["company"], q)]
    if not hits:
        print("NONE")
        return 0
    print(f"MATCH: {len(hits)}")
    for r in hits[:3]:
        print(f"  {r['company']}  ({r.get('reason', '')})")
    return 0


def cmd_add(args: list[str]) -> int:
    if not args:
        print("usage: skipped.py add <company> [reason]")
        return 2
    company = args[0]
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    _ensure_csv()
    if any(_company_matches(r["company"], company) for r in _rows()):
        print(f"already skipped: {company}")
        return 0
    with CSV_PATH.open("a", newline="") as f:
        csv.writer(f).writerow([company, reason])
    print(f"OK added: {company}")
    return 0


def cmd_list(_args: list[str]) -> int:
    for r in _rows():
        print(f"{r['company']}  ({r.get('reason', '')})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: skipped.py {check|add|list} ...")
        return 2
    sub, rest = argv[1], argv[2:]
    if sub == "check":
        return cmd_check(rest[0] if rest else "")
    if sub == "add":
        return cmd_add(rest)
    if sub == "list":
        return cmd_list(rest)
    print(f"unknown subcommand: {sub}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
