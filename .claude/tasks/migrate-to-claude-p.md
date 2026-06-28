# Plan — finish migrating the remote auto-applier to `claude -p`

## Goal
On the Hetzner VPS, alongside the assistant bot, run a fully autonomous job-applier:
Jack texts a direction over Telegram → Claude finds/opens the job → applies end-to-end
→ solves captchas → reads the email verification PIN itself → confirms. Jack does nothing.

## Current state (verified 2026-06-20)
- **Running on VPS**: `claude-applier` tmux session, `claude --channels plugin:telegram`,
  cwd `/home/claude/auto-applier`, model opus-4-7. This is the **persistent-Channels**
  approach — the thing Jack broke. It is wedged at "new task? /clear to save 131.9k tokens"
  (context-bloated, stuck). Its log `/tmp/claude-sessions/claude-applier.log` is unusable
  TUI escape-sequence noise.
- **Three diverged repo copies**: local `~/Desktop/ProjectFiles/auto-applier` (bot.mjs era,
  yesterday's work, never deployed); VPS `/root/auto-applier` (legacy); VPS
  `/home/claude/auto-applier` (running, older lineage, still has legacy Python `bot/`).
- **bot.mjs** (local, untracked): spawns `claude -p --output-format text <msg>` per Telegram
  message. The migration target. Gaps: no auth allowlist (replies to ANYONE), 300s timeout
  too short for email round-trips, fresh session per msg (no `--continue`), thin logging.
- **Email PIN retrieval is broken**: `scripts/check_email.cjs` (IMAP via imapflow) throws
  `IMAP_ERROR: Command failed`. Local `.env` points at Netsol IMAP for `homerfamily.com`,
  but applications use `jhomer191@gmail.com`, and the VPS copy expects a Gmail app password.
  Mismatched identity + broken connection. **This is the linchpin and must be fixed first.**
- **Captcha**: yesterday's claude wrote bespoke per-company scripts (`glean_apply.cjs`,
  `cursor_apply.cjs`) with stealth evasion + a manual `/tmp/glean_code.txt` polling hack for
  the PIN. Not generic, not autonomous. To be replaced by a generic playwright-MCP flow.
- **CLAUDE.md** is still written for the persistent-Channels model (immediate-ack via a
  telegram MCP tool that doesn't exist under `claude -p`, `/new /stop /queue /drain`, channel
  notifications). Must be rewritten for the `claude -p` model.

## Recommended architecture: `claude -p` per message via hardened bot.mjs
- `claude -p` uses the same OAuth/Max-subscription billing as interactive Claude — NOT Agent
  SDK API credits. Preserves the cost model that motivated the original SDK→tmux move; SDK
  would reintroduce per-token API billing. So `claude -p` beats the SDK here.
- Stateless-per-message kills context bloat (the wedged 131.9k-token session is the failure
  mode of persistent). Each application is self-contained; durable state lives in `data/` CSVs.
- One `claude -p` invocation does full single-job autonomy with a live browser: navigate
  (playwright MCP) → fill from profile.yaml → submit → captcha → email PIN (check_email.cjs)
  → enter → confirm → record. Clean greppable stdout/stderr logs, not TUI garbage.
- bot.mjs runs under **systemd** (not tmux), replacing the `claude-applier` Channels session.
  Assistant bot (`@Jackclaudecode_bot`) is untouched.

## Decisions (locked 2026-06-20)
1. **Architecture: `claude -p` via bot.mjs.** Per-message subprocess under systemd,
   subscription billing, replaces the Channels session.
2. **PIN inbox: `jack@homerfamily.com` (Netsol IMAP).** Applications must submit with this
   address (profile.yaml currently says jhomer191@gmail.com — must change). IMAP currently
   fails at CONNECT — almost certainly an expired/wrong `IMAP_PASS`. **Blocker: Jack must
   supply a fresh Netsol mailbox password** (or confirm the current one). Everything else
   downstream is wired and ready.
3. **Captcha: restore the lost stealth layer — do NOT add a solver.** ROOT CAUSE FOUND: the
   pre-tmux version wasn't getting caught because `bot/human.py` (git `a072bd4`/`7af98c5`)
   ran a `launch_stealth_context()` — randomized viewport/UA/locale/timezone, a
   webdriver-suppression init script (`navigator.webdriver=undefined`, fake plugins,
   hardwareConcurrency/deviceMemory), human-timed typing that fires real keyboard events
   (bypasses React bot checks), randomized mouse landing points, realistic delays, and a
   rate limiter. The migration to vanilla `@playwright/mcp` discarded all of it — MCP
   Playwright runs `navigator.webdriver=true` with no human emulation, so it started
   tripping captchas. Yesterday's `glean_apply.cjs` was a partial JS reinvention.
   **Fix: resurrect `human.py`'s stealth+human layer and apply through it, not raw MCP.**

## Steps
1. **Fix email PIN retrieval (linchpin).** With Jack's fresh Netsol password in `IMAP_PASS`,
   debug `check_email.cjs` to CONNECT_OK + fetch. Change profile.yaml application email to
   `jack@homerfamily.com` so PINs actually land in the box we read. Prove it: trigger a real
   verification email, read the code headlessly.
2. **Restore the stealth/human layer (captcha fix).** Recover `bot/human.py`'s
   `launch_stealth_context()` + human typing/mouse/delay logic from git `a072bd4`. Make the
   apply flow drive the browser through it (port to a `apply.cjs` helper, or restore the
   Python adapters). This is what kept the old bot from getting caught.
3. **Harden bot.mjs.** Auth allowlist (only `TELEGRAM_CHAT_ID`), raise timeout (~15 min for
   email waits), structured stdout/stderr logging to a file, `claude -p --resume` for optional
   continuity, clean output extraction. Keep the ⏳ working-indicator edit pattern.
4. **Rewrite CLAUDE.md for `claude -p`.** Drop telegram-MCP ack / `/new /stop /queue` /
   channel-notification sections (bot.mjs owns the transport now). Add the canonical
   autonomous single-job flow: stealth-context fill → captcha/human handling → poll
   check_email.cjs for the PIN → enter → confirm → `applied.py record`. Fix stale
   Gmail-vs-IMAP references (it's `jack@homerfamily.com` now).
5. **Delete the bespoke scripts.** Remove `glean_apply.cjs`/`cursor_apply.cjs` and the `/tmp`
   PIN hack; they're superseded by the stealth helper + check_email.cjs.
6. **Consolidate to one repo + deploy.** Commit local (it's the freshest) to main; rsync/clone
   to the VPS as the single source of truth; remove the legacy `bot/` Python and stale copies.
7. **Switch the runtime.** Stop the `claude-applier` Channels tmux session (free the
   `@Autoapplier_jackbot` token — two pollers collide). Install `auto-applier-bot.service`
   (systemd) running bot.mjs as the `claude` user; enable + start; confirm it answers Telegram.
8. **End-to-end proof.** From Telegram, send one real job URL; watch it apply, hit a PIN gate,
   read the code from email itself, confirm, and record — with Jack doing nothing. Capture the
   log as evidence.

## Risks / watchouts
- Two pollers on one bot token = dropped messages. Killing the Channels applier session is
  mandatory, not optional.
- @playwright/mcp launches a fairly vanilla browser; datacenter-IP bot detection may block some
  ATS portals (the glean script's stealth effort hints at this). Mitigation per captcha decision.
- Hard image captchas (reCAPTCHA v2 image / hCaptcha) are not reliably auto-solvable without a
  solver service — true "do nothing" autonomy on those needs a 2captcha-style API (decision 3).
- 300s→longer timeout: email PINs usually arrive <60s, but Greenhouse/Workday can lag.
- Never run `wg-quick`/`openvpn --redirect-gateway`/`iptables -F` on the VPS (SSH lockout).
- Solo repo → commit straight to main, no Co-Authored-By Claude footer (per Jack's rules).
