# Audit follow-up: back-port live code, usage-limit loop, nightly email sweep

Date: 2026-07-05. Approved by Jack (audit session).

## 1. Back-port /opt/auto-applier → this repo (live code was never committed)
- Copy from VPS: `discord_bot.mjs`, `CLAUDE.md` (live Jul 3 version wins), `.mcp.json`,
  `package.json`, new `scripts/*.cjs` (check_email, dump_email_body, get_*_code, get_verify_url),
  `.claude/settings.json` (the Bash lockdown), `systemd/auto-applier-brain.service`.
- Fix `.gitignore`: drop the blanket `*.json` (fragile; blocked committing .mcp.json/settings.json),
  ignore only the actually-secret/derived json (data/ already covers sessions).
- Commit straight to main (solo repo), push.

## 2. Usage-limit auto-resume loop (discord_bot.mjs)
- On usage-limit detection (already detected today, `USAGE_LIMIT_PAUSE`): parse reset time
  (epoch after `|`, or "resets …" phrase; fallback 30-min recheck — retry forever, NO caps).
- Enter paused state: hold the queue, schedule resume at reset+90s.
- On resume: synthetic continuation prompt ("limit reset; continue where you left off, check
  data/applied.csv for dupes") so a mid-run pause resumes the same session via --resume.
- `/status` shows paused-until; new messages during pause queue instead of failing.

## 3. Nightly email sweep + action-items sheet
- `scripts/nightly_sweep.mjs`: runs `claude -p` (same session mechanics, own prompt) to read
  the last ~48h of BOTH inboxes (Zach's Gmail IMAP + jack@homerfamily.com Netsol IMAP2),
  extract action items (recruiter replies, interview requests, "email X by Y"), maintain
  `data/action_items.csv` (date_added,due,who,contact,action,status,source), and post the
  open-items table to the #applications channel via Discord REST (bot token, no gateway).
- `systemd/applier-nightly.service` + `.timer` (daily 07:00 UTC ≈ 2-3am ET).
- Add IMAP2_* (Netsol, rotated password) to VPS `.env` (600, owner claude). Test login once.

## 4. Deploy
- rsync repo → /opt/auto-applier (exclude .env, data/), install timer.
- Restart auto-applier-brain only when idle (waiter script — do NOT kill the in-flight apply run).

## 5. VPS hygiene + local archive (approved)
- Remove claude-sessions watchdog cron; disable+delete stale units (auto-applier-bot,
  auto-applier-discord, claude-bot, claude-sessions); rm /root/auto-applier,
  /opt/auto-applier-bot, /opt/claude-bot, /opt/claude-sessions, /etc/claude-bot.env.
- Move Desktop/ProjectFiles/{claude-bot,claude-sessions} → Desktop/ProjectFiles/archive/;
  flag claude-bot/.env tokens for rotation.
