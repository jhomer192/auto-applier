#!/usr/bin/env python3
"""Credit every board that has produced an application with the hits it earned.

Why this exists: data/board_rotation.csv was cut to 200 breezy-only rows by the
cross-platform prune bug on 2026-08-08, taking the hit history of every greenhouse
and lever board with it. pick_boards() weights on hit RATE, so with no hits recorded
anywhere a wave is a uniform draw over 8,163 boards of which ~5% have ever produced
anything. Measured 2026-08-09: 800 boards mined at random returned 31,687 postings,
1,309 in his lanes, and 0 servable. Twice.

What it credits, and why that is honest: an application that landed is a hit by
definition, and the board was mined at least once to produce it. The denominator is
the part we genuinely lost, so every seeded board gets the SAME `mines` constant.
Being uniform, it cannot reorder boards against each other — a board with 27
applications still outranks one with 1. It only sets how hard the seeded cohort as a
whole is boosted, and at SEED_MINES=4 that is x1.25 for one application up to the
x2.25 ceiling at eight. This is deliberately not the rebuild-from-seen.csv that was
declined on 2026-08-09 morning: seen.csv rows are surfaced-not-served, and inventing
a rate from them would have biased the sampler toward over-exploiting.

Never overwrites a board that already carries real mining history. Idempotent.
Dry run by default; pass --write.

    python3 scripts/seed_rotation.py            # show what it would credit
    python3 scripts/seed_rotation.py --write
"""
from __future__ import annotations

import argparse
import collections
import csv
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source as S  # noqa: E402

# A board mined a handful of times is the prior. See the module docstring: uniform, so
# it sets the cohort's boost and never the order within it.
SEED_MINES = 4


def board_of(url: str) -> tuple[str, str] | None:
    """The (platform, token) a posting URL belongs to, in companies.txt's spelling.

    Returns None for career-site URLs that carry no ATS token (brex.com/careers), and
    for anything unrecognised. Guesses are cheap: main() keeps only what is in the pool.
    """
    parts = urllib.parse.urlsplit(url)
    host, path = parts.netloc.lower(), parts.path.strip("/").split("/")
    seg = path[0] if path and path[0] else ""
    if "greenhouse.io" in host:
        # boards.greenhouse.io/TOKEN/jobs/ID and job-boards.greenhouse.io/TOKEN/jobs/ID.
        # "embed" is the /embed/job_app?for=TOKEN form, where the token is in the query.
        if seg == "embed":
            for_ = urllib.parse.parse_qs(parts.query).get("for")
            return ("greenhouse", for_[0]) if for_ else None
        return ("greenhouse", seg) if seg else None
    if "lever.co" in host:
        return ("lever", seg) if seg else None
    if "ashbyhq.com" in host:
        return ("ashby", seg) if seg else None
    if host.endswith("breezy.hr"):
        sub = host.split(".")[0]
        return ("breezy", sub) if sub not in ("app", "www", "breezy") else None
    if host.endswith("recruitee.com"):
        return ("recruitee", host.split(".")[0])
    if host.endswith("teamtailor.com"):
        return ("teamtailor", host.split(".")[0])
    if "workable.com" in host:
        return ("workable", seg) if seg else None
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write data/board_rotation.csv")
    args = ap.parse_args(argv[1:])

    applied = S.APPLIED_PATH
    if not applied.exists():
        print(f"no {applied} — nothing to seed from", file=sys.stderr)
        return 1

    earned: collections.Counter = collections.Counter()
    rows = unparsed = 0
    with applied.open(newline="") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            rows += 1
            board = board_of(url)
            if board:
                earned[board] += 1
            else:
                unparsed += 1

    pool = set(S.load_pool())
    state = S.load_rotation()
    in_pool = {b: n for b, n in earned.items() if b in pool}
    off_pool = len(earned) - len(in_pool)

    seeded = skipped = 0
    for board, hits in sorted(in_pool.items(), key=lambda kv: -kv[1]):
        s = state.get(board)
        if s and (s.get("mines") or s.get("hits")):
            skipped += 1  # it has real history; that beats a reconstruction
            continue
        state[board] = {"last_mined": "", "mines": SEED_MINES, "hits": hits,
                        "fails": 0, "posts": 0}
        seeded += 1

    print(f"applied.csv: {rows} rows, {unparsed} with no ATS token in the URL")
    print(f"boards that produced an application: {len(earned)} "
          f"({len(in_pool)} in the pool, {off_pool} career sites not in it)")
    print(f"seeding {seeded} boards at mines={SEED_MINES}; "
          f"{skipped} left alone because they already have real history")
    top = sorted(in_pool.items(), key=lambda kv: -kv[1])[:10]
    for b, n in top:
        w = 0.25 + min(2.0, 4.0 * n / SEED_MINES)
        print(f"    {n:3} applications  {b[0]}:{b[1]:<28} productivity x{w:.2f}")
    if not args.write:
        print("\nDRY RUN — rerun with --write")
        return 0

    bak = S.ROTATION_PATH.with_suffix(".csv.bak-seed")
    if S.ROTATION_PATH.exists():
        bak.write_text(S.ROTATION_PATH.read_text())
        print(f"backed up {S.ROTATION_PATH} -> {bak}")
    S.save_rotation(state)
    print(f"wrote {S.ROTATION_PATH}: {len(state)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
