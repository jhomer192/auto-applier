"""Discord entrypoint for the auto-applier.

Launches the conversational ApplierAgent (Claude-as-the-bot + job tools). The bot
acts ONLY when messaged in #applications; there are no background pollers and no
email-send path. Apply-time verification-PIN reading (scripts/check_email.cjs) is
the only mailbox access, made on demand during an active application.

Run: python -m bot.main_discord
Env: DISCORD_BOT_TOKEN, JACK_USER_ID, DISCORD_CHANNEL_ID, APPLICANT_USER_IDS,
     PROFILE_PATH, DB_PATH, APPLIER_ENV_PATH, IMAP_* (for PIN auto-read).
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

from bot.agent_discord import ApplierAgent
from bot.db import ApplicationDB
from bot.profile import ProfileError, load_profile

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(format=_LOG_FMT, level=logging.INFO, force=True)
for _noisy in ("discord", "discord.client", "discord.gateway", "discord.http", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Rotating file log so runtime activity is always inspectable (the journal buffers).
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
        logger.error("Missing required env var %s "
                     "(need DISCORD_BOT_TOKEN, JACK_USER_ID, DISCORD_CHANNEL_ID)", e)
        raise SystemExit(1)

    applicant_ids = {
        int(t) for t in os.getenv("APPLICANT_USER_IDS", "").replace(",", " ").split() if t.isdigit()
    }
    env_path = os.getenv("APPLIER_ENV_PATH", ".env")
    profile_path = os.getenv("PROFILE_PATH", "profile.yaml")
    db_path = os.getenv("DB_PATH", "data/applications.db")

    try:
        profile = load_profile(profile_path)
    except ProfileError as e:
        logger.error("Profile error: %s", e)
        raise SystemExit(1)

    db = ApplicationDB(db_path)
    asyncio.run(db.init())

    bot_data = {"db": db, "profile": profile, "profile_path": profile_path}
    client = ApplierAgent(
        channel_id=channel_id, jack_id=jack_id, bot_data=bot_data,
        applicant_ids=applicant_ids, env_path=env_path,
    )
    if applicant_ids:
        logger.info("scoped applicants: %s", sorted(applicant_ids))
    logger.info("applier-agent starting (candidate=%r)…", profile.get("name"))
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
