# Wider job sourcing (2026-07-19)

## Problem
Each session sources jobs the same way — web search + the same handful of well-known
boards — so it resurfaces jobs already in `seen.csv`/`applied.csv` and runs dry. There
is no rotation state, so "which companies did we mine last time" is not remembered
across sessions and the brain re-mines its favourites every wave.

## Fix
`scripts/source.py` — a deterministic sourcing CLI the brain runs at the start of every
wave instead of web-searching:

1. **Big pool** — `scripts/companies.txt` (`platform:token`, a few hundred
   Greenhouse/Lever/Ashby boards), editable without touching code.
2. **LRU + weighted-random rotation** — `data/board_rotation.csv` tracks
   `last_mined`/`hits`/`fails` per board. Each run samples companies weighted by how
   long since they were last mined (Efraimidis–Spirakis), so consecutive sessions
   cover different companies. Dead tokens (repeated 404s) auto-prune.
3. **Location** — Bay Area (existing `bot.bay_area.is_bay_area`) **or** fully-remote-US
   (Jack, 2026-07-19). Non-US / other-metro still dropped.
4. **Lane-tiered, jittered ranking** — priority lanes (GRC/Security, SDR at security
   cos, IT support, Recruiting/People) rank first, random within tier.
5. **Dedup** against `applied.csv` + `seen.csv` via `url_norm.normalize`.
6. Output TSV `url  company  title  location  lane` for direct fit-filtering.

## Steps
- [x] Decide location scope (Jack: Bay Area + fully-remote-US)
- [ ] `scripts/companies.txt` — expanded pool
- [ ] `scripts/source.py` — rotation, fetch, filter, dedup, output
- [ ] Verify: run it, confirm live URLs + that two consecutive runs return different companies
- [ ] Update `CLAUDE.md` sourcing section to make it step 0 of every wave
- [ ] Commit to main (solo repo), deploy note for the VPS
