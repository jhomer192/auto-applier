# Auto Job Applier — Setup Guide

You are Claude Code, setting up the Auto Job Applier for the first time.
Follow every step exactly. Run all commands. Ask the user for input only where explicitly instructed (marked **ASK USER**).
Do not skip steps. Do not ask the user to run commands themselves.

---

## Step 1 — Verify Python 3.11+

Run: `python3 --version`

If the version is below 3.11, run:
```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils
```
Then verify again.

## Step 2 — Install system dependencies for Playwright Chromium

Run:
```bash
sudo apt-get install -y \
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
  libcairo2 libgdk-pixbuf2.0-0 libgtk-3-0
```

## Step 3 — Create virtual environment and install Python dependencies

Run:
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Confirm all commands exit 0 before continuing.

## Step 4 — Create the data directory

Run:
```bash
mkdir -p data/screenshots
```

## Step 5 — Build the user's profile

**ASK USER**: Tell them: "I'm going to ask you some questions to set up your job application profile. Take your time — your answers will be used exactly as you provide them on every application."

Then run:
```bash
.venv/bin/python setup/collect_profile.py
```

Follow the prompts interactively. When the script finishes, confirm that `profile.yaml` was created.

## Step 6 — Configure Telegram

**ASK USER**: "Go to @BotFather on Telegram, send /newbot, follow the prompts, and paste your bot token here."

Save their answer as `BOT_TOKEN`.

**ASK USER**: "Now:
1. Send any message to your new bot in Telegram.
2. Open this URL in your browser: https://api.telegram.org/bot{BOT_TOKEN}/getUpdates
3. Find the `id` field inside `result[0].message.from` — that's your chat ID. Paste it here."

Save their answer as `CHAT_ID`.

Copy `.env.example` to `.env` and fill in the values using sed:
```bash
cp .env.example .env
sed -i "s|TELEGRAM_BOT_TOKEN=|TELEGRAM_BOT_TOKEN=<BOT_TOKEN>|" .env
sed -i "s|TELEGRAM_CHAT_ID=|TELEGRAM_CHAT_ID=<CHAT_ID>|" .env
```

Verify the `.env` file looks correct before continuing.

## Step 7 — LinkedIn login (optional)

**ASK USER**: "Do you want to enable LinkedIn Easy Apply? This requires logging in to LinkedIn in a browser window. Reply yes or no."

If yes:
- **ASK USER**: "You'll need a display for this step. Are you using a VPS with a virtual display (e.g. DISPLAY=:0 configured), or do you have a desktop environment?"
- If they have a display: run `DISPLAY=:0 .venv/bin/python setup/linkedin_login.py`
- If they don't: tell them LinkedIn support will be unavailable for now, and they can run this step later with `DISPLAY=:0 python setup/linkedin_login.py`

If no: skip this step.

## Step 8 — Install the systemd service

Get the absolute repo path:
```bash
pwd
```

Get the Python executable path:
```bash
.venv/bin/python -c "import sys; print(sys.executable)"
```

Update the service file placeholders with the actual paths:
```bash
sed -i "s|REPO_PATH_PLACEHOLDER|$(pwd)|g" auto-applier.service
sed -i "s|PYTHON_PATH_PLACEHOLDER|$(pwd)/.venv/bin/python|g" auto-applier.service
```

Install and enable the service:
```bash
sudo cp auto-applier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now auto-applier
```

Check that the service started:
```bash
sudo systemctl status auto-applier
```

## Step 9 — Send a test message

Run:
```bash
.venv/bin/python setup/test_telegram.py
```

If this exits 0: tell the user "Setup complete! Your bot is running. Send a job URL from LinkedIn Easy Apply, Greenhouse, or Lever to your Telegram bot to apply automatically."

If this exits non-zero: show the error output and tell the user to re-check their bot token and chat ID in `.env`, then re-run the test.

---

## Daily use

- Send a job URL to your Telegram bot to apply.
- Reply Y to confirm, N to skip.
- Use /status to see application counts.
- Use /history to see past applications.
- Logs: `sudo journalctl -u auto-applier -f`
- Restart: `sudo systemctl restart auto-applier`

## Updating your profile

Edit `profile.yaml` directly. Changes take effect on the next bot restart:
```bash
sudo systemctl restart auto-applier
```

## Adding a new job site

See `bot/adapters/base.py` for the `SiteAdapter` Protocol. Implement the three methods, add your adapter to `bot/adapters/__init__.py`, and restart the service.
