# Restore board productivity so mining stops being a lottery

Measured 2026-08-09, two independent 400-board samples off the real pool:

    800 boards mined -> 31,687 postings -> 1,309 in-lane -> 0 servable

Zero, twice. Three causes, in order of size:

1. **The pool is mostly not his market.** `discover_boards.py` enumerates board
   tokens from the Wayback CDX index, which knows nothing about what a company
   does. Largest board in the sample: `greenhouse:svetness` (mobile pet grooming)
   at 4,975 postings = 26.7% of everything fetched. The top 5 boards were 41.7%
   of all fetch volume. Median board is 8-10 postings, p90 is 65. So 95.2% of
   postings die at `classify()` — correctly; they are groomers, vet techs and
   CSRs at street addresses.
2. **The productivity memory is empty.** `board_rotation.csv` is 200 breezy rows,
   all zero-hit — the residue of the cross-platform wipe fixed earlier today.
   `pick_boards()` weights on hit rate, so with no hits recorded anywhere every
   wave is a uniform draw over 8,163 boards of which ~5% have ever produced.
   That is why the "weighted" and "random" samples gave identical results.
3. Location is the last gate and is mostly load-bearing: of the 1,309 in-lane,
   ~40% genuinely foreign, ~29% pinned to a non-CA state, ~12% CA but not Bay.
   Only a ~1% sliver (bare `Remote`, no US token) is arguably a false reject.

## Plan

1. `posts` column on rotation state — postings returned by a board, cumulative.
   Written in the mining loop next to `mines`.
2. `_is_live()` drops a board with `posts >= BARREN_POSTS (400)` and `hits == 0`:
   it has returned p99 volume across every mine it has ever had and never once
   surfaced a job in any lane. `hits` never decays, so `hits == 0` really does
   mean never-ever, and `--stats` already names every pruned board.
3. `scripts/seed_rotation.py` — one-shot. Credits each board in `applied.csv`
   with `hits = applications it produced` and a uniform `mines = SEED_MINES`.
   The constant is a documented prior, identical for every seeded board, so it
   never reorders them against each other; it only sets how hard the seeded
   cohort as a whole is boosted. Never overwrites a board that already has real
   history. This is the honest version of the rebuild declined this morning:
   an application that landed is a hit by definition, whereas seen.csv rows are
   surfaced-not-served and have no denominator at all.

## Not doing

- No sweep of all 8,163 boards to pre-classify them barren. That is a mining
  wave's worth of requests off Jack's home IP; the rule in (2) learns the same
  thing from ordinary waves at zero extra cost.
- Nothing gets turned on. Still no restart, still no armed auto-wave.
