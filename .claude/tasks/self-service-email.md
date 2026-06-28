# Task: self-service email for applicants (zvessey)

## Goal
Let a scoped applicant (zvessey) submit/update their own email creds from the
#applications Discord channel so the auto-applier applies *as them* and reads
their verification PINs — without Jack handling the password.

## How email is consumed today
- Forms: `profile.yaml` `email:` is what gets typed into application forms.
- Apply-time PIN: `scripts/check_email.cjs` reads `IMAP_HOST/PORT/USER/PASS` from `.env`.
- So "apply as zvessey + read his PINs" = point BOTH `profile.yaml email` and the
  `.env` IMAP_* at zvessey's mailbox.

## Design
New command `/email <address> <app-password> [imap-host]` in #applications.
1. **Authz:** applicants (zvessey) may use ONLY `/email`; Jack keeps full command
   set. Add `APPLICANT_USER_IDS` env → `applicant_ids` set; on_message gate allows
   jack ∪ applicants; dispatch rejects any non-`/email` command from an applicant.
2. **Validate + test:** resolve IMAP host from the address domain (gmail→imap.gmail.com,
   outlook, yahoo, homerfamily→netsol; optional explicit 3rd arg). Actually log in
   over IMAP (imaplib in a thread) BEFORE saving — reject bad creds.
3. **Store securely:** on success, atomically rewrite `.env` IMAP_USER/IMAP_PASS/
   IMAP_HOST/IMAP_PORT (chmod 600) and set `profile.yaml email:` to the address.
4. **Hygiene:** DELETE the invoking Discord message (it contains the password) right
   after processing, success or fail. Never log the password. Reply with a summary
   that does not echo it. Best-effort rebuild of the live `bot_data["gmail_inbox"]`;
   note the recruiter-poll loop fully adopts new creds on next restart (apply-time
   PIN path via check_email.cjs is immediate since it reads .env fresh per run).

## Files
- `bot/email_setup.py` (new) — host resolution, IMAP login test, atomic .env +
  profile.yaml writes. Pure-ish, unit-testable (host resolution + env rewrite).
- `bot/discord_frontend.py` — `applicant_ids`, gate, `/email` dispatch + msg delete.
- `bot/main_discord.py` — read `APPLICANT_USER_IDS`, pass env/profile paths + ids.
- `.env.example` — document `APPLICANT_USER_IDS`.

## Verify
- Offline: host resolution table; .env rewrite round-trip preserves other keys +
  mode 600; profile.yaml email swap keeps the rest of the YAML.
- Gate unit: applicant→only /email; jack→all; stranger→deny.
- Live: zvessey posts `/email …` → bot tests login, replies ok/fail, deletes his
  message; `.env`/`profile.yaml` now his. Then a real apply uses his email + PIN.
