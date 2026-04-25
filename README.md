# Auto Job Applier

Finds jobs, writes tailored applications, and submits them while you sleep. You set your target roles and a score threshold — the bot searches LinkedIn every 30 minutes, analyzes each posting with Claude, and auto-applies to anything above your threshold. Everything else comes to you as a Y/N prompt in Telegram.

---

## Install

SSH into a Ubuntu VPS (a $6/month [DigitalOcean](https://digitalocean.com) Droplet works great), then:

```bash
git clone https://github.com/jhomer192/auto-applier.git && cd auto-applier && bash setup.sh
```

The setup wizard handles everything: Python deps, your candidate profile, Telegram bot, LinkedIn login, systemd service, and autonomous mode configuration. Takes about 10 minutes.

> **No VPS yet?** Follow [Part 1–3](#part-1--get-a-server) below — it walks you through creating one even if you've never used a terminal.

---

## How it works

```
You set:  /prefs roles Software Engineer,Backend Engineer
          /prefs autoapply 80

Bot does: ┌─ every 30 min ─────────────────────────────────────┐
          │ 1. Search LinkedIn for your target roles            │
          │ 2. Score each posting (fit, salary, sponsorship)    │
          │ 3. Score ≥ 80 → fill form + submit, notify you     │
          │ 4. Score < 80 → send Y/N prompt to Telegram         │
          │ 5. Hard pass → silently skip                        │
          └────────────────────────────────────────────────────┘
```

---

## Beginner setup guide

This section assumes you have never used a terminal, never set up a server, and have no idea what any of this means. That's fine. Every step is numbered, every command is copy-paste, and every confusing moment is called out ahead of time.

> **Grad students and recent grads:** This bot was built with you in mind. During setup it asks about your research areas, thesis, publications, and whether you need visa sponsorship. It uses that context to write cover letters that bridge your academic work to industry roles — the hardest part of academic-to-industry applications. It also auto-skips jobs that explicitly say "no sponsorship" if you need it.

---

## Features

### Apply from a URL
Send any LinkedIn Easy Apply, Greenhouse, or Lever job URL to Telegram. The bot analyzes the posting, generates a tailored resume and cover letter for that specific job, and asks if you want to apply. You see a summary before committing:

```
Software Engineer at Acme Corp (via Greenhouse)
Location: Remote | Salary: $120k–$150k
Match score: 87/100

⚠  No Go/Rust experience mentioned — minor gap
Cover letter preview: "I was drawn to Acme's work on distributed..."

Reply Y to apply, N to skip
```

### Smart auto-decisions
- **Auto-apply** — set a score threshold (e.g. 80). If the match is above it, the bot submits without waiting for your Y.
- **Auto-skip** — add companies to an exclude list and the bot silently passes on them. If the salary is far below your floor, it skips automatically.

### Background job search
Set up saved searches and the bot polls job boards on your behalf. When new listings appear that match your criteria, it pings you in Telegram.

### Gmail integration
The bot watches your inbox for recruiter emails. When one arrives, you get a Telegram notification with a preview. Type your reply in Telegram and the bot sends it as a proper email reply. Rejections and application confirmations are silently filtered — only interview requests and job offers trigger a notification.

### Retrieve past materials
Use `/resume <id>` or `/coverletter <id>` to pull up the tailored resume or cover letter that was generated for any past application.

### Profile building
The `/profile` command runs a short achievement interview. Your answers are used to personalize every resume and cover letter the bot generates.

During setup, the bot also asks about your **academic background** — research areas, thesis topic, publications, GPA, and degree program. Cover letters are written to connect your research to the specific problems the role works on, not just list skills. If you need visa sponsorship, set `/prefs sponsorship yes` and the bot will hard-skip any job that explicitly declines to sponsor.

### Full anti-detection
Human-like typing and mouse movement, randomized browser fingerprints, rate limiting, and configurable daily application caps.

---

## What you'll need (and what it costs)

| Thing | Cost | Why you need it |
|---|---|---|
| DigitalOcean account | $6/month | A computer in the cloud that runs the bot 24/7, even when your laptop is off |
| Claude Pro or Max plan | $20 or $100/month | The AI brain that reads job postings and fills out forms |
| Telegram app | Free | How you send URLs to the bot and get back results |

| Plan | Monthly cost | Best for |
|---|---|---|
| Claude Pro + DigitalOcean $6 | ~$26/month | Grad students applying to 5–15 jobs/day |
| Claude Max + DigitalOcean $6 | ~$106/month | Heavy users, no usage limits |

**Claude Pro works for most grad students. If you hit usage limits, upgrade to Max.** You can cancel either service any time.

---

## Part 1 — Get a server

You need a computer that is always on and always connected to the internet. You don't buy one — you rent a tiny virtual one from DigitalOcean for $6/month. This type of rented computer is called a **server** or a **VPS** (Virtual Private Server). You never touch it physically. You control it by typing commands into a terminal window on your own computer.

### Step 1 — Create a DigitalOcean account

1. Go to [digitalocean.com](https://www.digitalocean.com) and click **Sign Up**
2. Enter your email and create a password
3. Verify your email address (check your inbox)
4. Add a payment method when prompted — this is required before you can create anything

### Step 2 — Create your server (called a "Droplet")

DigitalOcean calls their servers "Droplets." Here's how to create one:

1. Once you're logged in, click the green **Create** button in the top right, then click **Droplets**
2. **Choose a region** — pick the city closest to you. This affects response speed, though for this bot it barely matters
3. **Choose an image** — click **Ubuntu**, then make sure **22.04 (LTS) x64** is selected. If you see a newer version, that's fine too
4. **Choose a size** — click **Basic**, then scroll to find the **$6/month** option (1 GB RAM / 1 CPU / 25 GB disk). That's all this bot needs
5. **Choose authentication** — this is how you'll log in to your server. For beginners, the easiest option is **Password**. DigitalOcean will email you a root password after the server is created. If you know what SSH keys are and have one set up, use that instead — it's more secure
6. Leave everything else as the default
7. Scroll down and click **Create Droplet**

You'll see a progress bar for about 30 seconds. When it's done, you'll see your new Droplet listed with a green dot and an **IP address** — a number that looks like `143.198.57.22`. **Copy that IP address and keep it somewhere.** You'll use it in the next step.

> **What's an IP address?** It's your server's home address on the internet — a unique number that tells other computers where to find it.

---

## Part 2 — Connect to your server

You're going to open a terminal window on your own computer and type a command to connect to your server. Once you're connected, anything you type runs on the server, not your own computer. This is called an **SSH connection** (Secure Shell — it's an encrypted tunnel between your computer and the server).

### Step 3 — Open a terminal

**On a Mac:**
1. Press `Command + Space` to open Spotlight
2. Type `Terminal` and press Enter
3. A window with a text prompt appears — you're in the terminal

**On Windows:**
- **Windows 10/11:** Press the Windows key, type `Windows Terminal`, and open it. If it's not installed, search for it in the Microsoft Store (it's free)
- **Older Windows:** Download [PuTTY](https://www.putty.org) (free). Open it, enter your server's IP address in the "Host Name" field, and click Open

**On Linux:**
- You know how to open a terminal

### Step 4 — Connect to your server

In your terminal, type this command — replace `YOUR_IP_ADDRESS` with the actual number you copied from DigitalOcean:

```bash
ssh root@YOUR_IP_ADDRESS
```

For example, if your IP is `143.198.57.22`, you'd type:

```bash
ssh root@143.198.57.22
```

Press Enter.

**You'll see something like:**

```
The authenticity of host '143.198.57.22 (143.198.57.22)' can't be established.
ED25519 key fingerprint is SHA256:abc123xyz...
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

> **Don't panic — this is normal.** Your computer is saying "I've never talked to this server before, do you trust it?" Since you just created this server yourself, type `yes` and press Enter. You'll only see this message the first time you connect.

Next, you'll be asked for a password. If you chose **Password** authentication in DigitalOcean, check your email — DigitalOcean sent you a temporary root password. Paste it in. **Note: when you type or paste a password in a terminal, nothing appears on screen — no dots, no asterisks. This is intentional.** Just paste and press Enter.

You may be asked to change your password immediately. If so, follow the prompts — enter the old password once, then your new password twice.

**You'll know you're connected when you see something like:**

```
root@your-droplet-name:~#
```

That `#` at the end is your prompt. Everything you type from here runs on your server.

---

## Part 3 — Install Claude Code

Claude Code is the tool that makes all of this work. It's an AI assistant that runs in your terminal and will handle the entire setup process for you. You just answer its questions.

### Step 5 — Install Node.js

Claude Code requires Node.js (a software runtime). Run these two commands one at a time — paste each line, press Enter, and wait for it to finish before doing the next:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
```

```bash
sudo apt-get install -y nodejs
```

> **What's happening?** The first command downloads an installer script and runs it. The second command uses that installer to actually put Node.js on your server. This can take a minute or two.

### Step 6 — Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

> **`npm` is Node's package manager** — it's like an app store for developer tools. The `-g` means "install this globally so it works from anywhere."

This will take 30 seconds to a minute. You'll see a lot of text scroll by — that's normal.

### Step 7 — Log in to Claude Code

```bash
claude login
```

This will print a URL. Copy it and open it in a browser on your computer. It will take you to Anthropic's website and ask you to sign in to your Claude account (the same account your Max plan is attached to). Once you approve it in the browser, your terminal will say something like `Logged in as your@email.com` and you're good to go.

> **Why does Claude Code need your account?** The bot uses Claude's AI to read job postings and decide what to write in each form field. That AI usage runs against your Max plan, which includes enough usage for this.

---

## Part 4 — Install the bot

### Step 8 — Run the setup wizard

```bash
git clone https://github.com/jhomer192/auto-applier.git && cd auto-applier && bash setup.sh
```

The script will:
1. Install Python 3.11+ and all dependencies automatically
2. Ask you questions to build your candidate profile (work history, skills, education, etc.)
3. Walk you through creating a Telegram bot via BotFather and connecting it
4. Optionally set up LinkedIn Easy Apply (requires a one-time browser login)
5. Install the bot as a background service (starts automatically on reboot)
6. Ask if you want autonomous mode (auto-apply threshold + role search)

**Answer honestly and completely** — your answers become your application profile. The bot never invents or adds anything; it only uses what you give it.

The whole setup takes about 10 minutes.

---

## Part 5 — What to expect during setup

At some point during setup, the script will ask you to create a Telegram bot. Here's what that looks like so you're ready.

### Step 9 — Create your Telegram bot via BotFather

1. Open Telegram on your phone (or at [web.telegram.org](https://web.telegram.org) on your computer)
2. In the search bar, search for **@BotFather** — it has a blue checkmark. Tap on it
3. Tap **Start** if you haven't talked to it before
4. Send the message `/newbot`
5. BotFather will ask for a **name** — this is the display name people see. Type something like `My Job Applier` and send it
6. BotFather will ask for a **username** — this must end in `bot` and be unique across all of Telegram. Try something like `yourname_jobapplier_bot`
7. BotFather will send you a message containing a long string of letters and numbers — something like `7291847362:AAGkd8fj29FkdjsI02kfJDkf83kdlsf`

> **What's a token?** It's a secret password that proves you own this bot — paste it when Claude Code asks for it.

**Keep this token private.** Anyone who has it can control your bot.

Claude Code will also ask for your **chat ID** — a number that tells the bot to send messages specifically to you. It will give you exact instructions for finding it at the time, so don't worry about it now.

---

## Part 6 — How to use it

Once setup is done, your bot is running in the background on your server. You don't need to leave any terminal windows open. The bot will keep running even if you close everything and turn off your laptop.

### Sending a job to apply for

1. Open Telegram and find your new bot
2. Send it a job URL — any of these formats work:
   - `https://linkedin.com/jobs/view/...`
   - `https://boards.greenhouse.io/...`
   - `https://jobs.lever.co/...`
3. The bot will reply with a match summary and ask **Y or N**

**You'll see something like:**

```
Software Engineer at Acme Corp (via Greenhouse)
Location: Remote | Salary: $120k–$150k
Match score: 87/100

⚠  No Go/Rust experience mentioned — minor gap
Cover letter preview: "I was drawn to Acme's work on distributed..."

Reply Y to apply, N to skip
```

4. Reply **Y** to apply or **N** to skip
5. If you reply Y, the bot fills out the form and sends you a screenshot when it's done

> **What if it asks me something before submitting?** If the application form contains a question not covered by your profile — like "describe a time you showed leadership" — the bot will ask you before it submits. Just reply in Telegram and it will continue.

### Autonomous mode

Enable this and the bot finds and applies to jobs with no input from you:

```
/prefs roles Software Engineer, Backend Engineer
/prefs autoapply 80
/prefs autosearch on
```

- `/prefs autosearch on|off` — auto-generate LinkedIn searches from your desired roles (default: on)
- `/prefs autoapply <0-100>` — auto-submit jobs scoring ≥ this threshold; 0 = always ask Y/N

### Job search commands

- `/search add <query>` — manually add a search (e.g. `/search add backend engineer`)
- `/search add <query> in <location>` — with location (e.g. `/search add backend engineer in Austin`)
- `/search list` — show all active searches
- `/search rm <id>` — pause a search
- `/queue` — review pending discovered jobs, pick which to investigate
- `/report` — application stats (today/week/all-time) + queue size + top companies

### Preference commands

- `/prefs roles <role1>, <role2>` — desired job titles (drives auto-search)
- `/prefs salary <min> [target <t>]` — salary floor and optional target
- `/prefs seniority <level>` — seniority preference (junior/mid/senior/staff/principal)
- `/prefs arrangement <remote|hybrid|onsite>` — work arrangement preference
- `/prefs exclude <company>` / `/prefs unexclude <company>` — hard-pass list
- `/prefs pace <min> <max>` — gap in minutes between applications
- `/prefs dailycap <n>` — maximum applications per day
- `/prefs sponsorship yes|no` — hard-skip jobs that explicitly won't sponsor visas
- `/prefs show` — display all current preferences

### Profile, branding, and history

- `/profile` — run the achievement interview to update your profile
- `/linkedin [url]` — audit your LinkedIn profile with scored section-by-section feedback
- `/website [minimal|dark|academic]` — generate a self-contained GitHub Pages portfolio site
- `/website guide` — step-by-step deploy instructions
- `/resume <id>` — retrieve the tailored resume for a past application
- `/coverletter <id>` — retrieve the cover letter for a past application
- `/status` — total application count
- `/history` — recent applications with dates and scores
- `/cancel` — dismiss the current pending item
- `/help` — full command reference

---

## Common problems and fixes

**"Permission denied" when connecting via SSH**
This usually means the wrong username or the wrong password. Make sure you're using `root` (not your name or anything else) and that the password is exactly what DigitalOcean emailed you. Passwords are case-sensitive.

**"ssh: connect to host ... port 22: Connection refused"**
Your Droplet might still be starting up. Wait 60 seconds and try again. If it keeps failing, go back to your DigitalOcean dashboard and confirm the Droplet shows a green "active" dot.

**Nothing appears when I type my password**
This is normal behavior in terminals. The cursor doesn't move and nothing is shown, but your keystrokes are being recorded. Type the password and press Enter.

**The bot stopped responding after a day or two**
SSH into your server and run:

```bash
sudo systemctl status auto-applier
```

If it shows "failed" or "inactive," restart it:

```bash
sudo systemctl restart auto-applier
```

**"I replied Y but nothing happened"**
Check the logs:

```bash
journalctl -u auto-applier -f
```

This shows live log output. Look for any error messages in red. Press `Ctrl+C` to stop watching logs.

**I used /cancel and my recruiter email notifications stopped**
`/cancel` dismisses the current pending item (one at a time). The bot polls for new emails every 5 minutes — if more are waiting, you'll be prompted for the next one. Rejections and application confirmations are silently filtered; only interview requests and job offers trigger a notification.

**I want to update my profile (new job title, new skills, etc.)**
You can run `/profile` from Telegram to update it interactively, or SSH into your server and edit the file directly:

```bash
cd auto-applier
nano profile.yaml
```

Make your edits in `nano`, press `Ctrl+X`, then `Y`, then Enter to save. Then:

```bash
sudo systemctl restart auto-applier
```

**My auto-apply threshold isn't working**
Use `/prefs show` to confirm the `autoapply` value is set to what you expect. A value of 0 means auto-apply is disabled. Make sure you've also set your role and salary preferences — the bot needs those to compute a match score.

**My cover letter doesn't mention my research**
Make sure you answered the academic background questions during setup. If you skipped them, add an `academic:` section to `profile.yaml` with the following fields: `university`, `department`, `degree`, `research_areas` (a list), and optionally `thesis`. Then restart the service:

```bash
sudo systemctl restart auto-applier
```

**Bot applied to a job that won't sponsor visas**
Run `/prefs sponsorship yes` in Telegram. The bot will hard-skip future jobs that explicitly decline to sponsor. Jobs that don't mention sponsorship will still come through with a warning.

---

## You're done

**Verify the install:** `.venv/bin/pytest --tb=short -q` — all tests should pass.

Once setup is complete, you never need to touch the server again unless something breaks. The bot runs 24/7, you send it job URLs from your phone, and it handles the rest.

If you get stuck at any step and can't figure it out, the most useful thing you can do is copy the exact error message you're seeing and search for it online — most terminal errors have been seen by thousands of people and have clear answers.
