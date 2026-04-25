# Manual Test Checklist

Run these before declaring the auto-applier production-ready.

## Setup
- [ ] Clone repo on fresh VPS (Ubuntu 22.04)
- [ ] Open Claude Code in repo directory
- [ ] Verify Claude Code runs CLAUDE.md setup start to finish without errors
- [ ] Confirm profile.yaml is created with correct data
- [ ] Confirm .env is populated
- [ ] Confirm systemd service starts: `systemctl status auto-applier`

## Telegram bot
- [ ] Test message arrives in Telegram after setup
- [ ] /help shows command list
- [ ] /status shows "No applications yet."
- [ ] /cancel with no pending job says "Cancelled."
- [ ] Sending non-URL text shows helpful message
- [ ] Sending URL from non-authorized user ID is silently ignored

## Greenhouse application
- [ ] Send a boards.greenhouse.io job URL
- [ ] Bot replies with job title and company
- [ ] Reply Y -> bot says "Analyzing..."
- [ ] Bot submits and sends screenshot
- [ ] /status shows 1 applied
- [ ] DB record has correct submitted_fields JSON

## Lever application
- [ ] Same flow as Greenhouse with a jobs.lever.co URL

## LinkedIn Easy Apply
- [ ] Run linkedin_login.py, complete login
- [ ] Send linkedin.com/jobs/view/... URL
- [ ] Bot steps through Easy Apply modal and submits
- [ ] Confirmation screenshot received

## NEEDS_USER_INPUT flow
- [ ] Find a job with a field not in profile (e.g. "Security clearance")
- [ ] Bot asks for that specific field before submitting
- [ ] Answering the question completes the application

## Error handling
- [ ] Send an unsupported URL (e.g. workday.com) -> "Unsupported site" message
- [ ] Kill the bot process -> systemd restarts it within 10 seconds
- [ ] Check logs: `journalctl -u auto-applier -f`

## Gmail Inbox Flow

Prerequisites: `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` set in `.env`, bot restarted.

### Inbound notification
- [ ] Send a test email to the configured Gmail address with subject "Interview Invitation — Test Role"
- [ ] Within 5 minutes, Telegram bot sends a notification with the from address, subject, and preview
- [ ] Notification shows the availability prompt (not a plain "you got mail" message)

### Reply flow
- [ ] Type availability (e.g. "Tuesday 2-4pm or Thursday any time") in Telegram
- [ ] Bot replies "Composing reply..." then echoes the composed email body
- [ ] Check sent Gmail — email appears in Sent with correct In-Reply-To header (opens in same thread as original)

### /cancel dismisses one email
- [ ] Send two interview emails to Gmail
- [ ] Wait for both notifications to arrive
- [ ] Send /cancel — bot dismisses first, immediately prompts for second
- [ ] Send /cancel again — bot replies "Dismissed." and queue is empty

### Filtering
- [ ] Send email with subject "Thank you for applying" — no Telegram notification (confirmation filtered)
- [ ] Send email with body "Unfortunately we will not be moving forward" — no Telegram notification (rejection filtered)
- [ ] Send email with subject "We'd like to extend a job offer" — notification arrives with 🎉 header and offer prompt

### Offer flow
- [ ] Reply "accept" to an offer notification — Claude composes acceptance email, bot sends and echoes it
- [ ] Reply "counter at $120k, start March 1" to an offer — Claude composes counter-offer email professionally
