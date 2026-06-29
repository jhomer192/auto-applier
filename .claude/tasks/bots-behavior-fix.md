# Plan: Make all three Discord bots behave the way Jack expects

Source of truth: the live Discord chat across #general, #classistant, #applications,
#bot-collab (read 2026-06-29). Jack's north star, in his words:
> "I want the auto applier to be more like talking to a normal bot but it just has
>  some extra tools." / "The way the auto applier worked on telegram was so much
>  better… I just sent it off to go apply and it just had information about the
>  applicant." / "fix all the bots to behave how I expect."

## Root cause (one theme, three symptoms)
classistant + coder are Claude Agent SDK bots — **Claude is the brain, MCP tools are
the hands** (`claude-discord/bot.py`). The applier is the odd one out: a Python state
machine that maps every message → a job hunt (`discord_frontend.py:157`). So:
- It can't converse, answer, or remember — only hunt.
- A question ("did you apply to all of them?") becomes a new search.
- When the apply engine hit the Claude usage limit, 23 calls returned instantly and
  were reported as "❌ failed" instead of "paused — usage limit."
classistant/coder problems are narrower: missing a read-history tool, an over-eager
"act on everything" disposition, and a coder init failure on the box.

## Part 1 — Applier becomes a conversational bot (the big one)
Rebuild the **frontend only** onto the claude-discord Agent-SDK pattern. The proven
apply pipeline (`claude -p` + Playwright at /opt/auto-applier) and all safety
invariants are reused unchanged — only the Python chat state machine is replaced.

- Applier runs as a Claude Agent SDK bot (like classistant/coder). Claude is the brain.
- Its "extra tools" (in-process MCP):
  - `find_jobs(query)` → wraps `job_boards.find_board_jobs` (+ finder fallback).
  - `apply_to_job(url)` / `apply_to_jobs(urls)` → wraps `mcp_apply.apply_via_mcp`
    (keeps the Bay-Area gate + apply-as-the-candidate prompt — server-side, NOT
    promptable away).
  - `application_status()` → reads the applications DB so "did you apply to all?" is
    answered from real data.
  - `set_email(addr, app_password)` → existing `email_setup.submit_email` (self-serve).
- Honest usage-limit handling: when an apply returns the usage-limit signal, STOP the
  batch and say "paused — Claude usage limit, resets <when>", not 23 fake failures.
- Memory per channel (Agent SDK session) so it can answer "why'd you stop?" truthfully.
- INVARIANTS PRESERVED (enforced inside the tools, not the prompt):
  - Bay-Area-only (`bay_area.is_bay_area` + `BAY_AREA_RULE`).
  - Applies as the candidate in profile.yaml/resume.pdf (Zachary), never Jack.
  - NO email send path; only the apply-time PIN reader. On-demand only — no pollers.
  - Scoped-applicant gate (zvessey may talk to it; only in #applications).
- Delete the dead Python scaffolding it replaces (telegram_bot state machine, the
  command table, transport shims) — git history keeps it. No stubs.

## Part 2 — classistant behaves (read the room, stay in lane)
- System-prompt overhaul: don't treat venting/questions as action items; answer first,
  act only when asked; **stay in your lane** — job applications are the applier's job,
  don't jump into #applications or offload to coder unprompted; be concise, cut the
  "standing by / still here" filler.
- Add a `read_channel_history(channel_id, limit)` MCP tool in `discord_tools.py` so it
  can actually read another channel when Jack says "read the conversation in X" —
  instead of asking him to paste.
- Don't auto-watch channels it was told to look at once; watching is deliberate.

## Part 3 — coder actually runs
- Diagnose on the VPS: `CLIConnectionError: working dir missing` +
  `Control request timeout: initialize`. Likely a missing WORKDIR, a stale
  CLAUDE_CODE_OAUTH_TOKEN, or an Agent-SDK/CLI version mismatch for the coder unit.
- Fix root cause (env/unit/token), restart, confirm it answers a ping.

## Part 4 — bot-to-bot hygiene (lower priority)
- The message-crossing + redundant filler in #bot-collab: tighten via the shared
  system-prompt guidance (don't post "standing by" no-op turns; one substantive turn or
  stay silent). Keep the existing 6-hop limiter.

## Verification (live, via Playwright Discord, as Jack)
- Applier: "did you apply to all of them?" → it ANSWERS from the DB (no new hunt).
  "find + apply to bay area SOC jobs" → finds + applies as Zach. A non-Bay job → BLOCKED.
  Force a usage-limit path → it pauses honestly.
- classistant: ask it to read #applications history → it uses the new tool. Vent at it →
  it reads the room, doesn't spawn work or ping coder.
- coder: @coder ping → real reply, no init error.
- Confirm invariants: no email-send path reachable; applies go out as Zachary; Bay-only.

## Repos / deploy
- `auto-applier` (solo → commit to main, author Jack alone): Parts 1.
- `claude-discord` (solo → main): Parts 2, 4, and the coder fix wiring.
- Deploy to VPS claude-server (5.78.207.54): applier unit `auto-applier-discord`,
  bots `claude-discord@assistant` / `@coder`. Commit often per Jack.
