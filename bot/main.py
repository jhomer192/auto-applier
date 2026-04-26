import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from bot.adapters import AdapterRegistry
from bot.db import ApplicationDB
from bot.inbox import GmailInbox
from bot.profile import load_profile, load_preferences, ProfileError
from bot.voice import load_voice_profile
from bot.auto_apply import ensure_auto_searches, process_queued_jobs
from bot.scam_detector import check_scam
from bot.sources import ALL_SOURCES
from bot.telegram_bot import AutoApplierBot, notify_new_emails, notify_search_matches

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INBOX_POLL_INTERVAL = int(os.getenv("INBOX_POLL_INTERVAL", "300"))      # seconds (5 minutes)
SEARCH_POLL_INTERVAL = int(os.getenv("SEARCH_POLL_INTERVAL", "1800"))   # seconds (30 minutes)
SOURCE_POLL_INTERVAL = int(os.getenv("SOURCE_POLL_INTERVAL", "3600"))   # seconds (1 hour)


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
    """Background task: seed searches, find new jobs, auto-apply — every SEARCH_POLL_INTERVAL seconds."""
    logger.info("Search poller started (every %ds)", SEARCH_POLL_INTERVAL)
    while True:
        try:
            db: ApplicationDB = app.bot_data["db"]
            profile: dict = app.bot_data["profile"]

            # 1. Auto-seed searches from desired_roles (no-op if already exist or auto_search=False)
            new_searches = await ensure_auto_searches(db, profile)
            if new_searches:
                logger.info("Auto-seeded %d new saved searches from desired_roles", new_searches)

            # 2. Run all active searches, queue new matches
            await notify_search_matches(app, linkedin_auth)

            # 3. Process the queue: auto-apply where threshold is met, batch the rest
            await process_queued_jobs(app, linkedin_auth)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Search poll error: %s", e)
        await asyncio.sleep(SEARCH_POLL_INTERVAL)


async def _sources_poll_loop(app) -> None:
    """Background task: poll external job discovery sources every SOURCE_POLL_INTERVAL seconds.

    Runs all active sources (GitHub new-grad repos, company Greenhouse/Lever boards,
    GitHub org discovery) and enqueues any new jobs matching the user's desired roles.
    Already-seen URLs are deduplicated via the seen_jobs table.
    """
    logger.info("Sources poller started (every %ds)", SOURCE_POLL_INTERVAL)
    while True:
        try:
            db: ApplicationDB = app.bot_data["db"]
            profile: dict = app.bot_data["profile"]
            prefs = load_preferences(profile)
            keywords = list(prefs.desired_roles) if prefs.desired_roles else []

            if not keywords:
                logger.debug("sources: no desired_roles configured — skipping poll")
            else:
                new_jobs = 0
                for source in ALL_SOURCES:
                    source_new = 0
                    try:
                        async for job in source.discover(keywords):
                            if await db.is_job_seen(job.url):
                                continue
                            scam = check_scam(job.url, job.title, job.company)
                            if scam.verdict == "rejected":
                                await db.insert_rejected_job(
                                    job.url, job.title, job.company,
                                    scam.score, "|".join(scam.signals),
                                )
                                await db.mark_job_seen(job.url, None)
                                logger.info(
                                    "sources: scam-rejected %s (score=%d)", job.url, scam.score
                                )
                                continue
                            elif scam.verdict == "flagged":
                                added = await db.enqueue_job(
                                    job.url, job.title, job.company,
                                    scam_score=scam.score,
                                    scam_flag=1,
                                    scam_signals="|".join(scam.signals),
                                )
                                await db.mark_job_seen(job.url, None)
                                if added:
                                    source_new += 1
                                    new_jobs += 1
                                continue
                            added = await db.enqueue_job(job.url, job.title, job.company)
                            await db.mark_job_seen(job.url, None)
                            if added:
                                source_new += 1
                                new_jobs += 1
                    except Exception as src_err:
                        logger.error("sources: error in %s: %s", source.name, src_err)
                    if source_new:
                        logger.info("sources: %s queued %d new jobs", source.name, source_new)

                if new_jobs:
                    logger.info("sources: total %d new jobs queued this cycle", new_jobs)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Sources poll error: %s", e)
        await asyncio.sleep(SOURCE_POLL_INTERVAL)


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
        linkedin_auth=linkedin_auth,
    )

    async def _post_init(application) -> None:
        # Pre-load voice profile into bot_data so handlers don't re-read the file each call
        application.bot_data["voice_profile"] = load_voice_profile()

        # Store HandshakeSource reference so /handshake command can read session state
        from bot.sources.handshake import HandshakeSource
        handshake_src = next((s for s in ALL_SOURCES if isinstance(s, HandshakeSource)), None)
        application.bot_data["handshake_source"] = handshake_src

        if gmail_inbox:
            application.create_task(_inbox_poll_loop(application, gmail_inbox))
        # Always start the search poller (it no-ops when there are no saved searches)
        application.create_task(_search_poll_loop(application, linkedin_auth))
        # Start the sources poller (GitHub repos, company boards, GitHub orgs)
        application.create_task(_sources_poll_loop(application))

    post_init = _post_init

    app = bot.build_app(post_init=post_init)

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
