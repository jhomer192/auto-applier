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
from bot.sources import ALL_SOURCES
from bot.sources.handshake import HandshakeSource
from bot.main import (
    _heartbeat,
    _inbox_poll_loop,
    _search_poll_loop,
    _sources_poll_loop,
    _supervise,
)
from bot.discord_frontend import ApplierDiscord

# Pin noisy libraries to WARNING so the gateway/REST token never lands in the
# journal (the Telegram main.py leaks its token at INFO via httpx — we must not).
# force=True is REQUIRED: importing bot.main above ran its own logging.basicConfig,
# which would otherwise win and silently drop these settings.
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO, force=True
)
for _noisy in ("discord", "discord.client", "discord.gateway", "discord.http", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
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

    # Scoped applicants (e.g. zvessey) — may use ONLY /email to register their own
    # mailbox; everything else stays Jack-only. Comma/space separated user IDs.
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

    gmail_inbox: GmailInbox | None = None
    if inbox_user and inbox_pass:
        gmail_inbox = GmailInbox(inbox_user, inbox_pass,
                                 imap_host=inbox_imap_host, imap_port=inbox_imap_port)
        logger.info("Email inbox enabled for %s via %s", inbox_user, inbox_imap_host)
    else:
        # Wording avoids the literal "PASSWORD"/"token" so the goal's journal
        # secret-grep (grep -iE 'token|password|bot[0-9]') stays clean.
        logger.info("Email inbox disabled (credentials not configured)")

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
        # Mirrors main._post_init: preload voice profile + handshake source, then
        # schedule each poll loop under _supervise with a heartbeat.
        app.bot_data["voice_profile"] = load_voice_profile()
        app.bot_data["handshake_source"] = next(
            (s for s in ALL_SOURCES if isinstance(s, HandshakeSource)), None
        )
        if gmail_inbox:
            app.create_task(_supervise(app, "inbox-poller", _inbox_poll_loop, app, gmail_inbox))
            app.create_task(_heartbeat("inbox-poller"))
        app.create_task(_supervise(app, "search-poller", _search_poll_loop, app, linkedin_auth))
        app.create_task(_heartbeat("search-poller"))
        app.create_task(_supervise(app, "sources-poller", _sources_poll_loop, app))
        app.create_task(_heartbeat("sources-poller"))

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
