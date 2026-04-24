# Auto Job Applier

Send a job URL to Telegram. Your bot applies automatically.

Supports **LinkedIn Easy Apply**, **Greenhouse**, and **Lever**.

---

## Requirements

- A VPS running Ubuntu 22.04+ (any $5/month instance works)
- [Claude Code](https://claude.ai/code) with a Max plan ($100/month)

---

## Setup

```bash
git clone https://github.com/jhomer192/auto-applier.git
cd auto-applier
claude
```

Claude Code reads the setup guide and walks you through everything:
- Installs dependencies
- Asks for your name, email, work history, skills
- Connects your Telegram bot
- Logs in to LinkedIn (optional)
- Starts the background service

The whole process takes about 10 minutes.

---

## How it works

```
You → Telegram bot → Claude analyzes job → Playwright fills form → Screenshot sent back
```

Every field is filled using only what you provided in your profile. Claude never invents
or embellishes anything. If a form asks for something not in your profile, the bot asks
you before submitting.

Applications are saved locally so you can track what was sent.

---

## Daily use

Send any job URL to your Telegram bot:

- `https://linkedin.com/jobs/view/...`
- `https://boards.greenhouse.io/...`
- `https://jobs.lever.co/...`

Reply **Y** to apply, **N** to skip.

**Commands:** `/status` · `/history` · `/cancel` · `/help`
