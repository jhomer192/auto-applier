# Raise qualified-application throughput

Goal: more applications per wave to jobs Zach could actually get. No behaviour
change to *when* the applier runs — it stays off, auto-wave stays disarmed.

Measured baseline (887 logged attempts since 2026-08-04):
  applied 162 (18%) | skipped 653 (74%) | blocked 43 (5%) | error 29 (3%)
  skip reasons: location 38%, closed/dead 27%, seniority 24% = 89%
  cost: 38 min and 222 tool calls per application

The ceiling was never the job market and never the captchas. Three quarters of
everything a wave opened in a browser was thrown away, and nine tenths of those
rejects were knowable from the employer's own ATS API before the page loaded.

## Shipped 2026-08-09

1. **One verify stage against the ATS single-job API** (`verify_rows`), wired
   into both the backlog and the mining path. Resolves liveness, the real posted
   location and the experience band in ONE request per job actually served —
   replacing the browser page load that used to discover the same three facts.
   Bounded (stops at `--n`), cached 24h in `data/verify_cache.json`, 4 workers,
   ~4-6 req/s. Fails OPEN: only an explicit 404/410 or a passed
   `application_deadline` is fatal, so a WAF or a timeout never costs a live job.
   New flags `--max-years N` (default 4, `0` disables) and `--no-verify`.
2. **seen.csv gained a `location` column.** `backlog_rows()` runs the real
   `location_ok()` instead of hardcoding `"location": "?"`. The 4,311 legacy rows
   migrate in place (header only) and read back as unknown, which routes them to
   verify. `seen.py mark` takes an optional 4th location argument.
3. **Cross-platform rotation wipe fixed.** `save_rotation(keep=)` was pruning
   against the CURRENT RUN's pool, so a `--platforms breezy` run on 2026-08-08
   cut `board_rotation.csv` from ~1,800 rows to 200 breezy-only ones and made a
   healthy 8,285-board pool read as exhausted. New `rotation_keep()` scopes the
   prune to the platforms actually mined.
4. **Liveness burst throttled.** The 16-thread `applier/1.0` sweep is what put
   the Hetzner IP behind Greenhouse's WAF on 2026-08-08 (406 for hours, four
   consecutive waves zeroed). Real browser UA; board fan-out 16 -> 8 workers.
5. **Platforms that have never accepted a submit are no longer served.**
   Measured over all 4,311 surfaced against every application that landed:
   greenhouse 22.7%, breezy 23.7%, lever 8.0%, ashby 5.4%, workable 0/147,
   smartrecruiters 0/7, linkedin 0/7. `SUBMIT_BLOCKED` drops workable,
   smartrecruiters, workday, icims and linkedin at serve time (145 backlog rows);
   the rest sort by `PLATFORM_CONVERSION` within their tier. Ashby stays servable
   — 13 did land — and simply ranks last. Rows stay in seen.csv, so deleting a
   name from that set is all it takes to start offering them again.
6. **Triage verdicts are no longer re-served.** `DEAD_MARKER_RE` caught the
   posting that VANISHED; `REJECT_NOTE_RE` now catches the one still up that a
   wave already read and ruled out — "not Bay Area", "requires 2+ years", "too
   senior", "LinkedIn required". 502 of 4,311 rows carry one. All 502 were read
   by eye before shipping; it matches 0 of the 896 rows in applied.csv.
7. **`_names_other_state` reads spelled-out states.** It only ever matched a
   COMMA-prefixed code, so "BDR Texas Colorado" and "SDR - Austin TX" inherited a
   country-wide location. Names only, never bare codes (OR/IN/ME/IT).
8. **`data/wave_prompt.txt` rewritten.** It claimed the retry queue "is empty by
   design / do not go looking for them" while 17 live entries sat in it, and
   quoted Aug 3-4 counts as current. It now quotes no counts at all — every
   number is on the tools' own stderr — and drops the "about 15 applications then
   stop" per-wave ceiling.

Net on the backlog: 1,332 triaged + 145 walled + 111 location-pinned drop before
any request, then verify rejects roughly two thirds of what is left (36 dead, 14
wrong location, 3 closed per 20 served) — all pre-browser.

## Not done, on purpose

- Nothing was turned on. `applier-hub` was never restarted, auto-wave is not
  armed, no wave was triggered.
- Response/outcome tracking — measurement, not throughput.
- **Rotation history was NOT rebuilt** for the wiped greenhouse/lever boards.
  `pick_boards` weights on hit RATE (hits/mines), and seen.csv records hits but
  not mines, and nothing at all about boards mined for zero hits. Reconstructing
  it would have to invent the denominator, which biases the sampler toward
  over-exploiting. Those boards now read as never-mined, take maximum staleness,
  and re-earn a real rate on the next few waves. That is the designed fallback.

## Verified

- 208 pytest pass (`tests/unit/test_source.py` + `test_profile.py`), including 51
  new cases covering platform_of, SUBMIT_BLOCKED, REJECT_NOTE_RE both ways,
  spelled-out states, and verify_rows' drop/fail-open/early-stop behaviour.
  `tests/unit/test_db.py` does not collect — missing `aiosqlite`, pre-existing
  and unrelated.
- `_verify_one` exercised live against real Anthropic Greenhouse and Lever
  postings: surfaced Sydney/Dublin/Washington DC and year bands 10/4/8/7, and
  caught a real 404.
- `source.py --backlog` run end-to-end on the live 4,311-row seen.csv, on the VPS.
- seen.csv header migration run on the live file: 4,311 rows intact.

## Flagged for Jack, not acted on

- `HANG_SILENCE_MS = 15 * 60 * 1000` in `/opt/applier-hub/discord_bot.mjs:336`
  SIGINT/SIGTERM/SIGKILLs a wave after 15 minutes of stream silence, and
  `bot.mjs:44` (the retired Telegram bot) does the same at 15m flat. Both are
  time caps around a Claude turn. Deleting the hub one would also restore the
  8h25m hang it was added for, which is its own throughput loss — his call.
- The first name appears in 5 tracked files of a PUBLIC repo (`CLAUDE.md`,
  `scripts/source.py`, `scripts/nightly_sweep.mjs`, `tests/unit/test_source.py`,
  `.claude/tasks/loop-and-nightly-email.md`) and in the git history behind them.
