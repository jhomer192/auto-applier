import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from bot.adapters import AdapterRegistry
from bot.db import ApplicationDB
from bot.inbox import GmailInbox
from bot.profile import load_profile, ProfileError
from bot.telegram_bot import AutoApplierBot, notify_new_emails, notify_search_matches

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INBOX_POLL_INTERVAL = 300   # seconds (5 minutes)
SEARCH_POLL_INTERVAL = 1800  # seconds (30 minutes)


async def _inbox_poll_loop(app, inbox: GmailInbox) -> None:
    """Background task: poll Gmail every INBOX_POLL_INTERVAL seconds."""
    logger.info("Inbox poller started (every %ds)", INBOX_POLL_INTERVAL)
    while True:
        try:
            new_threads = await inbox.poll()
            db: ApplicationDB = app.bot_data["db"]
            for thread in new_threads:
                await db.insert_email(thread)
            await notify_new_emails(app)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Inbox poll error: %s", e)
        await asyncio.sleep(INBOX_POLL_INTERVAL)


async def _search_poll_loop(app, linkedin_auth: str) -> None:
    """Background task: check saved job searches every SEARCH_POLL_INTERVAL seconds."""
    logger.info("Search poller started (every %ds)", SEARCH_POLL_INTERVAL)
    while True:
        try:
            await notify_search_matches(app, linkedin_auth)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Search poll error: %s", e)
        await asyncio.sleep(SEARCH_POLL_INTERVAL)


def main() -> None:
    load_dotenv()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    profile_path = os.getenv("PROFILE_PATH", "profile.yaml")
    db_path = os.getenv("DB_PATH", "data/applications.db")
    linkedin_auth = os.getenv("LINKEDIN_AUTH_STATE", "data/linkedin_auth.json")
    screenshot_dir = os.getenv("SCREENSHOT_DIR", "data/screenshots")

    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD", "")

    # Validate profile exists and is well-formed before starting
    try:
        profile = load_profile(profile_path)
    except ProfileError as e:
        logger.error("Profile error: %s", e)
        raise SystemExit(1)

    Path("data/screenshots").mkdir(parents=True, exist_ok=True)

    db = ApplicationDB(db_path)
    asyncio.run(db.init())

    registry = AdapterRegistry(linkedin_auth_state=linkedin_auth)

    gmail_inbox: GmailInbox | None = None
    if gmail_address and gmail_app_password:
        gmail_inbox = GmailInbox(gmail_address, gmail_app_password)
        logger.info("Gmail inbox enabled for %s", gmail_address)
    else:
        logger.info("Gmail inbox disabled (GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)")

    bot = AutoApplierBot(
        token=token,
        chat_id=chat_id,
        db=db,
        profile=profile,
        registry=registry,
        screenshot_dir=screenshot_dir,
        gmail_inbox=gmail_inbox,
        profile_path=profile_path,
    )

    async def _post_init(application) -> None:
        if gmail_inbox:
            application.create_task(_inbox_poll_loop(application, gmail_inbox))
        # Always start the search poller (it no-ops when there are no saved searches)
        application.create_task(_search_poll_loop(application, linkedin_auth))

    post_init = _post_init

    app = bot.build_app(post_init=post_init)

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
