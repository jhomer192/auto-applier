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
