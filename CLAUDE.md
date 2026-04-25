# Auto Job Applier

> **IMPORTANT — Read this first.**
> When a user opens this project in Claude Code, **immediately begin setup** by following
> the steps below. Do not wait for the user to ask. Do not say "I see you have a new
> project" or similar. Just start Step 1.
>
> Exception: if `profile.yaml` already exists in this directory, setup is done.
> Skip to the **Daily use** section at the bottom and tell the user their bot is ready.

You are helping set up an automatic job application bot. It will:
- Let the user send a job URL (LinkedIn Easy Apply, Greenhouse, or Lever) to Telegram
- Analyze the posting, score the match against the user's preferences, and generate a tailored resume + cover letter
- Auto-apply if the match score meets the user's threshold, or present a Y/N prompt with score, salary info, fit warnings, and cover letter preview
- Auto-skip jobs from excluded companies or with salaries far below the user's floor
- Poll saved job searches in the background and notify the user of new listings
- Watch Gmail for recruiter replies and let the user respond from Telegram

Follow every numbered step exactly. Run all commands yourself. Only stop and ask the user
for input where marked **→ ASK**.

---

## Step 0 — Verify Claude Code CLI is installed

Run: `claude --version`

If the command is not found, tell the user:
> "This bot requires Claude Code to be installed. Run `npm install -g @anthropic-ai/claude-code`
> and make sure `claude` is on your PATH, then come back."

Stop here until this is confirmed.

## Step 1 — Check Python

Run: `python3 --version`

If below 3.11:
```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils
```

## Step 2 — System dependencies for Playwright

```bash
sudo apt-get install -y \
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
  libcairo2 libgdk-pixbuf2.0-0 libgtk-3-0
```

## Step 3 — Install Python dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
mkdir -p data/screenshots
```

Confirm all commands exit 0 before continuing.

Then run a quick sanity check:
```bash
.venv/bin/pytest --tb=short -q
```
If tests pass, your environment is healthy.

## Step 4 — Build the profile

Tell the user: *"I'm going to ask you a few questions to set up your profile. Your answers
are used exactly as you provide them — I never invent or add anything."*

Then run:
```bash
.venv/bin/python setup/collect_profile.py
```

Wait for the script to finish. The script will finish by asking whether to set up the **Gmail inbox** feature (optional).
This enables Telegram notifications when recruiters reply to your applications, and lets you
respond directly from Telegram. If you skip it now, you can enable it later by adding
`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` to `.env` and restarting the service.

Confirm `profile.yaml` was created.

## Step 5 — Telegram bot

**→ ASK**: "Go to @BotFather on Telegram. Send `/newbot`, give it a name, and paste the
token it gives you here."

Save the answer as `BOT_TOKEN`.

**→ ASK**: "Now:
1. Send any message to your new bot in Telegram
2. Open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find the number next to `"id"` inside `result[0].message.from` — that's your chat ID.
   Paste it here."

Save as `CHAT_ID`.

Write `.env`:
```bash
cp .env.example .env
sed -i "s|TELEGRAM_BOT_TOKEN=|TELEGRAM_BOT_TOKEN=${BOT_TOKEN}|" .env
sed -i "s|TELEGRAM_CHAT_ID=|TELEGRAM_CHAT_ID=${CHAT_ID}|" .env
```

Show the user the resulting `.env` to confirm (mask the token to just the first 10 chars).

## Step 6 — LinkedIn login (optional)

**→ ASK**: "Do you want LinkedIn Easy Apply support? It needs a one-time login in a browser
window. Reply yes or no."

If **yes**: **→ ASK**: "Does your VPS have a display? (Either a desktop environment or
`DISPLAY=:0` set up via Xvfb?)"

- If yes → run: `DISPLAY=:0 .venv/bin/python setup/linkedin_login.py`
  A browser window will open. Tell the user to log in to LinkedIn. The script
  closes automatically once login is detected.
- If no → tell them: "Skipping LinkedIn for now. You can run
  `DISPLAY=:0 python setup/linkedin_login.py` later if you set up a virtual display."

If **no**: skip this step.

## Step 7 — Install as a background service

```bash
sed -i "s|REPO_PATH_PLACEHOLDER|$(pwd)|g" auto-applier.service
sed -i "s|PYTHON_PATH_PLACEHOLDER|$(pwd)/.venv/bin/python|g" auto-applier.service
sudo cp auto-applier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now auto-applier
sudo systemctl status auto-applier
```

Confirm the service status shows `active (running)`.

## Step 8 — Test

```bash
.venv/bin/python setup/test_telegram.py
```

If exit 0: tell the user:

> **Setup complete!** Your job application bot is running.
>
> **How to use it:**
> - Send any LinkedIn Easy Apply, Greenhouse, or Lever job URL to your Telegram bot
> - The bot evaluates fit, generates a tailored resume + cover letter, and asks Y/N
> - Reply **Y** to apply or **N** to skip — or let auto-apply handle it
> - The bot fills and submits the form and sends you a screenshot
>
> **Job search commands:**
> - `/search add <query> [in <location>]` — add a saved job search
> - `/search list` — show active searches
> - `/search stop <id>` — pause a search
>
> **Preference commands:**
> - `/prefs roles <role1>, <role2>` — set desired job titles
> - `/prefs salary <min> [target <t>]` — set salary floor/target
> - `/prefs seniority <level>` — set seniority preference
> - `/prefs arrangement <remote|hybrid|onsite>` — set work arrangement
> - `/prefs autoapply <score>` — auto-apply threshold (0 = disabled)
> - `/prefs exclude <company>` / `/prefs unexclude <company>`
> - `/prefs show` — display current preferences
>
> **Passive discovery commands:**
> - `/queue` — view pending discovered jobs, reply with numbers (e.g. "1,3") or "all" to investigate
> - `/report` — pipeline stats (today/week/all-time) + queue size + top companies
>
> **Profile, branding & history:**
> - `/profile` — run achievement interview to update profile
> - `/linkedin [url]` — audit LinkedIn profile (scored feedback per section)
> - `/website [minimal|dark|academic]` — generate a GitHub Pages portfolio
> - `/website guide` — step-by-step deploy instructions
> - `/resume <id>` — retrieve tailored resume for a past application
> - `/coverletter <id>` — retrieve cover letter for a past application
> - `/status` — see how many applications you've sent
> - `/history` — list recent applications with scores
> - `/cancel` — dismiss the current pending item
> - `/help` — show all commands
>
> **Logs:** `journalctl -u auto-applier -f`
> **Restart:** `sudo systemctl restart auto-applier`
> **Update profile:** run `/profile` in Telegram, or edit `profile.yaml` and restart

If non-zero: show the error and ask the user to re-check their bot token and chat ID in
`.env`, then re-run the test.

---

## Daily use

The bot is always running in the background. Send a job URL to Telegram to apply.

**Applying:** Send a LinkedIn Easy Apply, Greenhouse, or Lever URL. The bot evaluates the
match against the user's preferences and generates a tailored resume + cover letter. It
then presents a Y/N prompt showing match score, salary info, fit warnings, and a cover
letter preview. If `autoapply` is set and the score meets the threshold, it submits
without waiting. If the company is on the exclude list or salary is far below the floor,
it hard-passes automatically.

**Autonomous mode (set-and-forget):**
Set your desired roles and an auto-apply threshold and the bot handles everything:
1. Auto-generates LinkedIn searches from your desired roles (no `/search add` needed)
2. Every 30 minutes it polls for new matches and queues them
3. For each queued job: fetches the posting, scores the fit with Claude, fills the form
4. Jobs scoring ≥ threshold with all fields answerable from profile → submitted automatically, sends you a notification
5. Everything else → sent as a numbered batch review message for your Y/N

To enable:
```
/prefs roles Software Engineer,Backend Engineer
/prefs autoapply 80
/prefs autosearch on
```

- `/search add <query> [in <location>]` — add a manual saved search
- `/search list` — show active searches
- `/search rm <id>` — pause a search
- `/queue` — show pending jobs in the queue right now
- `/report` — pipeline stats (today/week/all-time), queue size, top companies

**Preferences:**
- `/prefs autosearch on|off` — auto-generate searches from desired roles (default: on)
- `/prefs roles <role1>, <role2>` — desired job titles (drives auto-search)
- `/prefs autoapply <score>` — auto-apply threshold; 0 = always ask Y/N
- `/prefs salary <min> [target <t>]` — salary floor and optional target
- `/prefs seniority <level>` — seniority level preference
- `/prefs arrangement <remote|hybrid|onsite>` — work arrangement preference
- `/prefs exclude <company>` / `/prefs unexclude <company>` — manage hard-pass list
- `/prefs show` — display all current preferences

**Profile, branding & history:**
- `/profile` — run achievement interview to refresh profile
- `/linkedin [url]` — audit your LinkedIn profile with scored section-by-section feedback
- `/website [minimal|dark|academic]` — generate a self-contained GitHub Pages portfolio HTML file
- `/website guide` — step-by-step instructions for deploying to GitHub Pages
- `/resume <id>` — retrieve tailored resume for a past application
- `/coverletter <id>` — retrieve cover letter for a past application
- `/status` — total application count
- `/history` — recent applications with dates and match scores
- `/report` — full pipeline stats + queue size + top companies
- `/cancel` — dismiss the current pending item (one at a time: job confirmation or recruiter email)
- `/help` — show all commands

**Gmail inbox:** If `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are set in `.env`, the bot
polls your inbox every 5 minutes. When a recruiter emails you, you get a Telegram
notification with a preview. Type your reply directly in Telegram and the bot sends it
via Gmail (as a proper reply thread). Rejections and application confirmations are
silently filtered — only interview requests and job offers trigger a notification.
Use `/cancel` to dismiss without replying.

To enable Gmail later without re-running full setup:
1. Generate an App Password at https://myaccount.google.com/apppasswords
2. Add to `.env`: `GMAIL_ADDRESS=you@gmail.com` and `GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx`
3. Restart: `sudo systemctl restart auto-applier`

**Adding a new job site:** see `bot/adapters/base.py` for the `SiteAdapter` interface.
Add your adapter to `bot/adapters/__init__.py` and restart the service.
