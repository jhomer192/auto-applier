"""Discord entrypoint for the auto-applier (replaces bot/main.py's Telegram polling).

Builds the same db/registry/inbox/profile state and schedules the same three
background poll loops, but runs them under a discord.py client + the transport
shims instead of python-telegram-bot. The apply/scrape/email logic is untouched.

Run: python -m bot.main_discord
Env (in addition to the existing PROFILE_PATH/DB_PATH/GMAIL_*/etc.):
  DISCORD_BOT_TOKEN   - the applier bot token
  DISCORD_CHANNEL_ID  - the #applications channel id
  JACK_USER_ID        - the only Discord user allowed to talk to it
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from bot.adapters import AdapterRegistry
from bot.db import ApplicationDB
from bot.inbox import GmailInbox
from bot.profile import ProfileError, load_profile
from bot.voice import load_voice_profile
from bot.discord_frontend import ApplierDiscord

# Pin noisy libraries to WARNING so the gateway/REST token never lands in the logs.
_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(format=_LOG_FMT, level=logging.INFO, force=True)
for _noisy in ("discord", "discord.client", "discord.gateway", "discord.http", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Also log to a rotating FILE so runtime activity is always inspectable — the
# systemd journal can lag/buffer, and a real log file is how you see when (and why)
# a hunt/apply breaks. Lives at data/applier.log in the working directory.
try:
    from logging.handlers import RotatingFileHandler
    _logdir = os.path.join(os.getcwd(), "data")
    os.makedirs(_logdir, exist_ok=True)
    _fh = RotatingFileHandler(os.path.join(_logdir, "applier.log"), maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter(_LOG_FMT))
    _fh.setLevel(logging.INFO)
    logging.getLogger().addHandler(_fh)
except Exception as _e:  # noqa: BLE001
    logging.getLogger("auto-applier-discord").warning("file logging unavailable: %s", _e)

logger = logging.getLogger("auto-applier-discord")


def main() -> None:
    load_dotenv()

    try:
        token = os.environ["DISCORD_BOT_TOKEN"]
        jack_id = int(os.environ["JACK_USER_ID"])
        channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
    except KeyError as e:
        logger.error(
            "Missing required env var %s — need DISCORD_BOT_TOKEN, JACK_USER_ID, DISCORD_CHANNEL_ID", e
        )
        raise SystemExit(1)

    # Scoped applicants (e.g. zvessey) — may register their own mailbox and run job
    # hunts for themselves; the legacy slash commands stay Jack-only. Comma/space ids.
    applicant_ids = {
        int(t) for t in os.getenv("APPLICANT_USER_IDS", "").replace(",", " ").split() if t.isdigit()
    }
    env_path = os.path.abspath(os.getenv("APPLIER_ENV_PATH", ".env"))

    profile_path = os.getenv("PROFILE_PATH", "profile.yaml")
    db_path = os.getenv("DB_PATH", "data/applications.db")
    linkedin_auth = os.getenv("LINKEDIN_AUTH_STATE", "data/linkedin_auth.json")
    screenshot_dir = os.getenv("SCREENSHOT_DIR", "data/screenshots")
    # Recruiter-reply inbox: prefer the IMAP_* creds (jack@homerfamily.com, the
    # address on the applications + the same account check_email.cjs reads for PINs),
    # falling back to the legacy GMAIL_* vars for back-compat.
    inbox_user = os.getenv("IMAP_USER") or os.getenv("GMAIL_ADDRESS", "")
    inbox_pass = os.getenv("IMAP_PASS") or os.getenv("GMAIL_APP_PASSWORD", "")
    inbox_imap_host = os.getenv("IMAP_HOST", "imap.gmail.com")
    inbox_imap_port = int(os.getenv("IMAP_PORT", "993"))

    try:
        profile = load_profile(profile_path)
    except ProfileError as e:
        logger.error("Profile error: %s", e)
        raise SystemExit(1)

    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)

    db = ApplicationDB(db_path)
    asyncio.run(db.init())

    registry = AdapterRegistry(linkedin_auth_state=linkedin_auth)

    # The continuous recruiter-inbox poller reads the WHOLE inbox and can auto-reply
    # to senders. That is NEVER appropriate for an applicant's personal mailbox, so
    # it is OFF by default and only runs if ENABLE_INBOX_POLL=1 is explicitly set.
    # Apply-time verification-PIN reading is SEPARATE (scripts/check_email.cjs, run
    # on-demand only during an active application) and does not depend on this.
    enable_inbox_poll = os.getenv("ENABLE_INBOX_POLL", "").strip().lower() in ("1", "true", "yes", "on")
    gmail_inbox: GmailInbox | None = None
    if enable_inbox_poll and inbox_user and inbox_pass:
        gmail_inbox = GmailInbox(inbox_user, inbox_pass,
                                 imap_host=inbox_imap_host, imap_port=inbox_imap_port)
        logger.info("Recruiter inbox poller ENABLED for %s", inbox_user)
    else:
        logger.info("Recruiter inbox poller OFF — no inbox reading or auto-replies (apply-time PIN read unaffected)")

    # Mirror build_app()'s bot_data so the unmodified handlers find their refs.
    bot_data: dict = {
        "db": db,
        "profile": profile,
        "registry": registry,
        "authorized_user_id": jack_id,
        "screenshot_dir": screenshot_dir,
        "gmail_inbox": gmail_inbox,
        "profile_path": profile_path,
        "linkedin_auth": linkedin_auth,
    }

    def build_loops(app) -> None:
        # On-demand only. This bot acts ONLY when messaged in #applications (find +
        # apply, in discord_frontend). All three legacy background pollers are
        # retired: the inbox poller must never auto-read/reply to the applicant's
        # mailbox, and the search/sources pollers were tuned for software-eng
        # new-grads, not this candidate. Apply-time verification-code reading is a
        # separate on-demand call (scripts/check_email.cjs) made during an apply.
        app.bot_data["voice_profile"] = load_voice_profile()
        app.bot_data["handshake_source"] = None

    client = ApplierDiscord(
        channel_id=channel_id, jack_id=jack_id, bot_data=bot_data, build_loops=build_loops,
        applicant_ids=applicant_ids, env_path=env_path,
    )
    if applicant_ids:
        logger.info("scoped applicants (email-only): %s", sorted(applicant_ids))
    logger.info("applier-discord starting...")
    client.run(token, log_handler=None)  # keep our logging config (no token in logs)


if __name__ == "__main__":
    main()
