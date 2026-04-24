import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from bot.adapters import AdapterRegistry
from bot.db import ApplicationDB
from bot.profile import load_profile, ProfileError
from bot.telegram_bot import AutoApplierBot

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    profile_path = os.getenv("PROFILE_PATH", "profile.yaml")
    db_path = os.getenv("DB_PATH", "data/applications.db")
    linkedin_auth = os.getenv("LINKEDIN_AUTH_STATE", "data/linkedin_auth.json")
    screenshot_dir = os.getenv("SCREENSHOT_DIR", "data/screenshots")

    # Validate profile exists and is well-formed before starting
    try:
        profile = load_profile(profile_path)
    except ProfileError as e:
        logger.error("Profile error: %s", e)
        raise SystemExit(1)

    Path("data/screenshots").mkdir(parents=True, exist_ok=True)

    db = ApplicationDB(db_path)
    # Init DB synchronously before the bot starts
    asyncio.run(db.init())

    registry = AdapterRegistry(linkedin_auth_state=linkedin_auth)

    bot = AutoApplierBot(
        token=token,
        chat_id=chat_id,
        db=db,
        profile=profile,
        registry=registry,
        screenshot_dir=screenshot_dir,
    )
    bot.run()


if __name__ == "__main__":
    main()
