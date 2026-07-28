# Second applier instance — Jack Homer

Goal: run TWO independent applier brains on the VPS, one per candidate.
- `#applications`      → the existing candidate (untouched)
- `#applications-jack` → Jack Homer (new)

Decisions (Jack, 2026-07-28):
- Reuse the SAME `applier#2015` bot token for both processes; each filters on its own
  `DISCORD_CHANNEL_ID`. No new bot, no OAuth/hCaptcha step.
- Targeting for Jack: FDE / AI engineer / SWE / solutions engineer. **Bay Area, hybrid or
  onsite, SF preferred. NOT fully-remote** (opposite of the sibling instance's rule, which allows remote-US).
- Résumé: Jack's real one (he sent it); profile built verbatim from it.
- Add a sibling channel, don't rename `#applications`.

## Steps
1. [x] Created `#applications-jack` = **1531683975603687484** in the applications category;
   VIEW_CHANNEL denied for the `applicant` role (1520854988555812977).
2. [x] `/opt/applier-jack` — code copy (no data/browser_profile/.playwright-mcp/backups), own
   `browser_launch.sh` + `.mcp.json` + data dir. Path-rewrote get_code.cjs / get_toast_code.cjs;
   deleted the sibling's one-off maintenance scripts (patch_*, scrub_*, janitor.sh, bot.mjs).
   Verified zero remaining `/opt/auto-applier` references.
3. [x] Résumé pipeline is **his real LaTeX**, not a generated one: `resume-src/*.tex` (copy of
   private jhomer192/resume) + `scripts/build_resumes.py` → `data/resume.pdf` +
   `data/resume_head_of_ai.pdf`. pdflatex was already on the box. Visually verified 1-page.
   `profile.yaml` written from his real résumé.
4. [x] `CLAUDE.md` rewritten for Jack (295 lines): 4 lanes (fde/ai/swe/solutions), Bay-Area
   onsite/hybrid ONLY with fully-remote dropped, C3 AI excluded, interim-TS guidance,
   PIN-gates-not-autonomous, no tailoring. Kept site routing + blocked-page/no-loop rules.
   Also retargeted `scripts/source.py` (LANES, EXCLUDE, EARLY_SIGNALS, `location_ok`).
5. [x] `auto-applier-jack.service` + memory drop-in (MemoryHigh=1300M, MemorySwapMax=1500M —
   soft throttle only, no hard cap, no time/turn/cost caps anywhere).
6. [x] Live: `Bot ready: applier#2015 | channel=1531683975603687484 | allowlist=1`. Sibling
   instance unaffected. Brain dry-run confirmed it reads Jack's identity, lanes, location rule
   and résumé selection correctly.

## Deploy model
`/opt/applier-jack` is canonical, same as the sibling — the public `auto-applier` repo holds
neither instance's brain or profile (PII). Deploy = rsync/scp from a session. Résumé refresh =
push to jhomer192/resume, then re-sync `resume-src/` and run `build_resumes.py` (the VPS has no
GitHub token — writing one there was blocked by the safety classifier, so the pull is manual).

## Constraints / gotchas
- 1.9GB RAM box: do NOT run both waves at once — headed Chromium peaks ~1.1GB each.
- Disk 81% used (7.1G free); the sibling instance's `browser_profile` is 889M and `.playwright-mcp` 571M,
  so the second profile dir will grow similarly. Prune before it bites.
- IMAP PIN autonomy needs a mailbox password we don't have → the instance runs without it
  (same as the sibling did before its mailbox was provisioned). PIN-gated forms get
  `retry.py mark`-ed and reported instead of blocking the wave.
- Never commit either candidate's profile.yaml (gitignored, VPS is source of truth).
