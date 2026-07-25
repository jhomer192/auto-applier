# Task: get the candidate's PII out of the public repo

## Problem
`jhomer192/auto-applier` is a PUBLIC repo. Tracked files carry the candidate's
real full name (13x in `CLAUDE.md`) and email address (2x). Phone, home city,
LinkedIn and employer are already safe — they live only in `profile.yaml` and
`scripts/make_resumes.py`, both gitignored.

This is a third party's PII, published without a reason to be published.

## Source of truth: profile.yaml, not .env
Jack suggested `.env`. Using `profile.yaml` instead, because `CLAUDE.md` line 3
already declares it "the only source of truth for the applicant's name, email
and details" — an identity guardrail built deliberately (apply as the candidate,
never as Jack). Copying name/email into `.env` would create a second source that
can drift out of sync with the first and quietly break that invariant.
Both files are gitignored, so either satisfies "no PII in git"; only one keeps a
single source of truth.

## Steps
1. `make_resumes.py`: read name / email / phone / linkedin / site from
   `profile.yaml` instead of hardcoding. Keep `RESUME_LOCATION` as a local
   override — the resumes intentionally read "San Francisco, CA" (relocating)
   while `profile.yaml` location stays the real current city.
2. Scrub tracked files to generic references ("the candidate", "the email in
   `profile.yaml`"): `CLAUDE.md`, `bot/email_setup.py`, and the three
   `.claude/tasks/*.md` files. Keep the identity guardrail wording forceful.
3. Sync `CLAUDE.md` + `make_resumes.py` to `/opt/auto-applier` (the live brain
   reads them there), regenerate the four PDFs, confirm byte-identical content.
4. Commit + push.

## Out of scope — needs Jack's call
Scrubbing HEAD does not remove anything from git history. The name and email
remain in every historical commit and stay reachable on GitHub. Two options:
- flip the repo private (instant, reversible, no history rewrite), or
- rewrite history + force-push (destructive, invalidates clones/forks).
Do not do either without asking.
