# REVISED PLAN — live applier + classistant + coder as specified (candidate = Zachary Vessey)
# (folds in the 3 fable plan-reviewers' findings)

## Verified ground truth (2026-07-03)
- LIVE applier = `auto-applier-brain.service` → `node /opt/auto-applier/discord_bot.mjs` (thin router that
  runs `claude -p --resume` in /opt/auto-applier; CLAUDE.md there is the behavior spec; router has a
  busy-lock + per-channel queue + single active child; `/status`,`/stop` are router-handled locally).
- /opt already Zach-ified: profile.yaml/resume.pdf = Zach; .env IMAP zachvessey16@gmail.com (login VERIFIED).
  Proxy 127.0.0.1:40000 (WARP) up. 176 applied/983 seen. Operator = thesaltfarmer (201215140622761984),
  channel 1519562454403776717; I'm logged into Playwright AS thesaltfarmer. classistant =
  /opt/claude-discord/bot.py ACTIVE. /root/auto-applier + local Desktop repo = STALE (never deploy).
- ARCHITECTURE DEVIATION (flag to Jack): bots-behavior-fix.md wanted invariants enforced server-side/
  not-promptable. Live arch enforces Bay-gate / apply-as-Zach / no-send via CLAUDE.md PROMPT TEXT only
  (claude -p has unrestricted Bash). We verify prompt-level behavior; note it is not a hard gate.

## Phase 0 — read-only recon + safety snapshot (no writes, no bot probes)
- Confirm bot IDLE: `/status` (router-handled) AND no `claude` child proc on VPS; abort edits if busy.
- Snapshot recoverable state OUTSIDE the tree → /root/backups/<ts>/: applied.csv, seen.csv,
  skipped_companies.csv, data/discord_sessions.json, and the live --resume session transcript
  (~/.claude/projects/-opt-auto-applier*/<session>.jsonl).
- Read consumers before editing: scripts/skipped.py (CSV schema), scripts/check_email.cjs (read-only?),
  and grep crons/systemd timers for any inbox poller (spec: on-demand only, no pollers).
- Inventory Jack-identity artifacts in /opt tree: profile.jack.bak.yaml, resume.jack.bak.pdf,
  CLAUDE.md.bak, discord_bot.mjs.bak, T1_HANDOFF.md, T1_REFERRAL_PACK.md, README Jack refs.
- Check coder: does a claude-discord@coder unit exist? status? Check /opt/claude-discord for the
  read_channel_history tool + Part-2 system-prompt language (answer-first / stay-in-lane / no filler).

## Phase 1 — read-only evidence (no writes)
- V4 (honest): confirm NO dedicated send tooling (grep clean) AND check_email.cjs only READS; state
  plainly it is prompt-level, not a hard gate. Confirm no inbox poller.
- Inspect ONLY post-2026-06-28 applied.csv rows + after_submit.png: transcribe the name/email on the
  submitted form into text; confirm = Zach. Note the 4 Jack-era pre-cutover rows (annotate/skip).
- Confirm /opt CLAUDE.md + profile.yaml lanes are broad (any-field entry), not Jack's SWE/SOC-only.

## Phase 2 — fixes, ONLY in a confirmed-idle window (atomic writes, backups OUTSIDE tree)
Reference wording = local Desktop CLAUDE.md (already written/reviewed) — adapt, don't recompose.
1. Remove Jack company exclusions: /opt CLAUDE.md "Excluded companies: C3 AI, Virtue AI" → none; and
   empty the C3 AI/Virtue AI rows in data/skipped_companies.csv via temp-file + atomic mv (never in-place).
2. Fix residual Jack-identity wording in /opt CLAUDE.md (e.g. "Read profile.yaml for Jack's details" →
   "the candidate Zachary's details"); keep "Jack" only where it means the human operator.
3. QUARANTINE all Jack-identity artifacts + existing .baks OUT of /opt to /root/backups/<ts>/ so the
   agent can't glob them. Do NOT create new .bak files inside /opt.
4. V5 fix: add honest usage-limit handling to discord_bot.mjs — detect claude's usage-limit result
   subtype / signature and post "⏸ paused — Claude usage limit (resets <when>)" instead of a generic
   "exited code N". (fable sub-agent patch; keep minimal.)
5. Reset the session so verification starts from the fixed spec: delete the channel's entry in
   data/discord_sessions.json AND restart auto-applier-brain (idle only) so the in-memory map reloads.

## Phase 3 — prove the edits GOVERN the live (new) session
- One probe (message the bot as thesaltfarmer): "Report only, do not search or apply: for this candidate,
  what companies are excluded and whose name/email goes on applications?" → expect Zach + no C3/Virtue.
  Quote the reply TEXT (Playwright snapshot) in-transcript.

## Phase 4 — behavior verification (live, log-tailed, evidence = quoted text)
- V1 status-from-data: baseline applied/seen counts + log tail; probe "Report ONLY, start no new search/
  apply: how many have you applied to for Zach and the last few?" → quote reply; show log window has no
  find/apply activity and counts unchanged.
- V2 Bay block (two-stage): (a) "Would you apply to <real NON-Bay, non-dup, entry role URL>? Explain,
  DO NOT act." → expect reasoned DROP; (b) only if needed, live drop test with log tail + ready to /stop
  before any submit; target must be account-walled + not in applied/seen. Quote DROP text + show no new
  applied.csv row / no apply run in log. Note where the Bay gate lives (prompt-only = deviation).

## Phase 5 — V3 MANDATORY: one controlled real apply as Zach (the only real proof)
- Pick ONE clean Bay-Area (or remote-US) entry role on Greenhouse/Lever/Ashby, dedup-checked vs
  applied/seen/skipped, not account-walled. Have the bot apply. Evidence in-transcript: the Discord
  transcript, the applied.csv row, and the identity (name+email) on the submitted form transcribed to
  text = Zach. Fold in the 7-hr-turn check: snapshot the Discord working message twice ~30s apart, quote
  both to prove live activity cadence is visible (not a silent multi-hour turn).

## Phase 6 — classistant + coder
- classistant V6: (i) vent/ask in its channel → reads room, no spawned work, no coder ping, no "standing
  by" filler; (ii) applications-domain request in its channel → defers to the applier, doesn't act;
  (iii) "read the history in <channel>" → uses a read-history tool. If the tool or Part-2 prompt is
  absent (Phase 0), FIX /opt/claude-discord (tool + system prompt) then re-verify. Quote reply text.
- coder: @coder ping → real reply, no init error. If unit missing/broken, diagnose (CLIConnectionError/
  init timeout per spec) and fix, or declare out-of-scope with explicit justification in-transcript.

## Phase 7 — durability + final gate
- Back-port the /opt doc/code fixes into the canonical git repo (so future deploys don't regress);
  /opt is not a git repo — record the diffs in the repo + commit (author Jack, no AI attribution).
- 5-round adversarial review loop (3 fable reviewers/round), READ-ONLY on live state + the in-transcript
  evidence (reviewers must NOT message the bot or trigger applies). Fold in every real fix each round;
  print all 5 rounds. Done only after 5 rounds, final clean, all acceptance criteria shown passing.

## Invariants
No turn/cost limit. Never apply as Jack. All /opt writes only at confirmed idle, atomic. One real apply
max for V3. Surface the prompt-level-enforcement deviation to Jack rather than silently re-architecting.
