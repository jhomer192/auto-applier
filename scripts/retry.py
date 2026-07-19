#!/usr/bin/env python3
"""retry.py — jobs we bounced off for a TRANSIENT reason, to be retried later.

Why this exists (2026-07-15): blocked jobs used to go into seen.csv, which is the
permanent dedup file. seen.csv means "don't re-investigate this" — so an Ashby spam
block, an hCaptcha, or a 500 error permanently buried a job that was still live and
still a good fit. 132 jobs were lost that way, including live Bay-Area SDR/GRC roles.

Split the two ideas:
  - seen.py  = we evaluated it and it is NOT applyable (closed, senior, wrong lane,
               wrong location). Permanent. Never revisit.
  - retry.py = it IS a good fit, we just couldn't get through the door today
               (captcha, spam block, rate limit, 5xx, dead-looking link). Revisit.

    python3 scripts/retry.py mark <url> <company> <title> <reason>
    python3 scripts/retry.py list [n]      # oldest first (default 25)
    python3 scripts/retry.py done <url>    # applied or confirmed dead — drop it
    python3 scripts/retry.py count
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from url_norm import normalize as _normalize_url  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "retry.csv"
HEADER = ["first_blocked", "last_blocked", "attempts", "url", "company", "title", "reason"]


def _ensure_csv() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


def _read() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def _write(rows: list[dict]) -> None:
    _ensure_csv()
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})


def cmd_mark(args: list[str]) -> int:
    if len(args) < 1:
        print("usage: retry.py mark <url> [company] [title] [reason]")
        return 2
    url = args[0]
    company = args[1] if len(args) > 1 else ""
    title = args[2] if len(args) > 2 else ""
    reason = args[3] if len(args) > 3 else ""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = _read()
    norm = _normalize_url(url)
    for r in rows:
        if _normalize_url(r.get("url", "")) == norm:
            r["attempts"] = str(int(r.get("attempts") or 1) + 1)
            r["last_blocked"] = now
            if reason:
                r["reason"] = reason
            _write(rows)
            print(f"OK requeued (attempt {r['attempts']})")
            return 0
    rows.append({
        "first_blocked": now, "last_blocked": now, "attempts": "1",
        "url": url, "company": company, "title": title, "reason": reason,
    })
    _write(rows)
    print("OK queued for retry")
    return 0


def cmd_list(args: list[str]) -> int:
    n = int(args[0]) if args and args[0].isdigit() else 25
    rows = _read()
    rows.sort(key=lambda r: r.get("last_blocked", ""))
    if not rows:
        print("EMPTY")
        return 0
    for r in rows[:n]:
        print(f"{r.get('attempts', '1')}x  {r.get('company', '')} | {r.get('title', '')} | "
              f"{r.get('url', '')} | last={r.get('last_blocked', '')[:10]} | {r.get('reason', '')}")
    if len(rows) > n:
        print(f"... {len(rows) - n} more (total {len(rows)})")
    return 0


def cmd_done(args: list[str]) -> int:
    if not args:
        print("usage: retry.py done <url>")
        return 2
    norm = _normalize_url(args[0])
    rows = _read()
    keep = [r for r in rows if _normalize_url(r.get("url", "")) != norm]
    if len(keep) == len(rows):
        print("not in retry list")
        return 0
    _write(keep)
    print("OK removed")
    return 0


def cmd_count(_args: list[str]) -> int:
    print(len(_read()))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: retry.py {mark|list|done|count} ...")
        return 2
    sub, rest = argv[1], argv[2:]
    if sub == "mark":
        return cmd_mark(rest)
    if sub == "list":
        return cmd_list(rest)
    if sub == "done":
        return cmd_done(rest)
    if sub == "count":
        return cmd_count(rest)
    print(f"unknown subcommand: {sub}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
