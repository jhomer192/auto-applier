# T1 Job Hunt — Handoff

Everything that can be set up automatically has been set up. This doc covers
the few things only you can do (account creation, browser logins) plus what
to expect day-to-day.

The architecture changed: **Claude Code itself is the agent now** (no Python
service, no systemd, no API key needed beyond the `claude` CLI). The bot
reads `profile.yaml` and `data/` CSVs, then drives Telegram and Playwright
directly.

## What's already done

| Item | Status |
|---|---|
| `profile.yaml` — fully tailored for FDE / Applied AI / Solutions Engineer hunt | ✅ created |
| `profile.yaml` includes `target_companies` (Tier S + Tier 1) and `preferred_platforms` | ✅ added |
| `data/resume.pdf` — copied from `../resume/resume.pdf` | ✅ in place |
| `.env` — scaffolded, only Telegram tokens left to fill | ✅ created |
| `T1_REFERRAL_PACK.md` — LinkedIn search URLs + drafted DMs for 14 target companies | ✅ created |

The resume is already FDE-positioned — lead bullet is the $10.75M USMC delivery.
Don't tailor per-job; the new architecture intentionally ships the base resume
to every application (per CLAUDE.md → "Don't tailor per-job. Don't regenerate.").

## What you have to do

### 1. Telegram bot — required (5 min)
- Open Telegram, message **@BotFather** → `/newbot` → name it (e.g. `JackJobBot`)
- BotFather replies with a token — paste into `.env` → `TELEGRAM_BOT_TOKEN=...`
- Send any message to your new bot
- Open in a browser (replace TOKEN):
  `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Find the number next to `"id"` inside `result[0].message.from`
- Paste into `.env` → `TELEGRAM_CHAT_ID=...`

### 2. Claude Code CLI — required (1 min, probably already done)
- `claude --version` should print a version
- If missing: `npm install -g @anthropic-ai/claude-code`
- The CLI handles Anthropic auth — no API key needed in `.env`

### 3. Python deps for the helper scripts (2 min)
```bash
cd /home/claude/workspace/auto-applier
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```
The `scripts/` helpers (`applied.py`, `seen.py`, `skipped.py`) are pure-Python and
already work; the venv is for Playwright + future things.

### 4. LinkedIn cookies — recommended (5 min)
LinkedIn from datacenter IPs trips the "infinite security check" if you log in
fresh. Easiest path is to **export cookies from your already-logged-in browser**:

1. Install **Cookie-Editor** Chrome extension
2. Open <https://www.linkedin.com/feed> while logged in
3. Click the extension → **Export → JSON**
4. Send the JSON to the bot in Telegram — Claude will save it to
   `data/linkedin_auth.json` in the right format

Skip this if you're fine applying only via direct apply on Greenhouse / Lever /
Ashby (which is where most T1 companies post anyway). Direct applies are stronger
signal than LinkedIn Easy Apply per CLAUDE.md anyway.

### 5. Gmail App Password — optional (5 min)
Lets the bot ping you in Telegram when recruiters reply.
- Go to <https://myaccount.google.com/apppasswords> → create one
- Add to `.env` → `GMAIL_APP_PASSWORD=<16-char password>`
- `GMAIL_ADDRESS=jhomer191@gmail.com` is already set

---

## How you actually use it

There's no service to start. The bot is **Claude Code itself**, talking to you
over Telegram from this VPS.

Once `.env` has the Telegram tokens, the existing telegram-bot harness running
on the VPS is already configured to message Claude — i.e. you message your
new bot, it routes to Claude, Claude reads `profile.yaml` + this repo, and
acts.

**Daily flows:**

| You send to Telegram | What happens |
|---|---|
| A job URL (LinkedIn / Greenhouse / Lever / Ashby) | Claude dedup-checks via `scripts/applied.py check`, decides fit, auto-applies for clear fits, asks Y/N for borderline |
| `go find me jobs` | Claude searches LinkedIn rotating through `desired_roles`, drops dupes / excluded companies, auto-applies clear fits, surfaces borderline ones with one-line takes |
| `go find me Anthropic FDE roles` | Same but scoped — Claude will hit the company careers page directly first, fall back to LinkedIn if needed |
| `skip <Company>` | Adds to `data/skipped_companies.csv` |
| A Cookie-Editor JSON blob | Saves it to `data/linkedin_auth.json` |

End-of-turn summary format Claude uses:
> *Applied: 3. Surfaced: 5. Skipped: 2 (excluded company).*

**Auto-apply criteria** (from CLAUDE.md — for visibility):
- Title is in `desired_roles`
- Work arrangement matches preference
- Location compatible
- No sponsorship conflict
- Platform is Greenhouse / Lever / Ashby / Workable / Wellfound (frictionless)

Anything else → Y/N. Your `auto_apply_threshold: 0` setting in profile.yaml is
informational; the new arch doesn't run a scoring script — Claude decides in-context.

---

## Daily workflow

| Time | What you do |
|---|---|
| Morning | Send `go find me jobs` to Telegram. Reply Y/N to whatever Claude surfaces. ~10 min. |
| | Borderline applies are usually 5–10/day during peak hiring. |
| Evening | Open `T1_REFERRAL_PACK.md`, pick 1 target company, send 5 referral DMs. ~15 min. |
| Weekly | Ask the bot for a pipeline summary ("show me what we've applied to this week"). |

Outreach response rate target: **30%+ reply, 1–2 referrals offered per 5 DMs**.
Application response rate target: **15%+ for cold apps, 40%+ for referred apps**.

---

## State files

All persistence is CSV under `data/`:

| File | What it holds |
|---|---|
| `data/applied.csv` | One row per submitted application |
| `data/seen.csv` | URLs surfaced but not applied (dedup so you don't re-investigate) |
| `data/skipped_companies.csv` | Hard-pass companies |
| `data/linkedin_auth.json` | Playwright storageState cookies |
| `data/resume.pdf` | Uploaded to every application |

Don't read these directly — they grow unbounded. Use the helpers:
```bash
python3 scripts/applied.py count
python3 scripts/applied.py recent 10
python3 scripts/skipped.py list
```

---

## Troubleshooting

**Bot doesn't respond in Telegram.**
Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The harness logs
to journalctl on the VPS.

**LinkedIn shows "infinite security check".**
Re-export cookies via Cookie-Editor and send the JSON again. WARP fallback exists
(setup/linkedin_login_via_warp.sh) but rarely needed.

**Applying to wrong roles.**
Edit `profile.yaml` `desired_roles:` or just tell the bot in chat — it will
update `profile.yaml` for you.

**Want to add / remove a target company.**
Tell the bot in chat, e.g. "add Replit to T1 targets" — it edits
`profile.yaml`'s `target_companies` block.

---

## What's specific to T1 (vs. stock auto-applier)

- **`profile.yaml`** — pre-tailored from your resume with FDE-shaped summary,
  full work history (C3 + Action Network), 5 projects (tableside, lowball-bot,
  Claude Bot, Wikipedia Game Solver, this bot), 38 skills, T1 keyword set
- **`profile.yaml` → `target_companies`** — Tier S (5) + Tier 1 (35) AI labs and
  AI-native startups, used by Claude to bias discovery and borderline decisions
- **`profile.yaml` → `preferred_platforms`** — Greenhouse / Lever / Ashby
  prioritized over LinkedIn Easy Apply (direct applies = stronger signal)
- **`T1_REFERRAL_PACK.md`** — LinkedIn search URLs + drafted DMs for 14 companies.
  ~10× callback rate vs cold apply.
