"""Autonomous job search and application pipeline.

Two entry points called from the background loop in main.py:

    ensure_auto_searches(db, profile)
        Creates saved searches from desired_roles automatically so the user
        doesn't have to run /search add manually.  No-ops if auto_search is
        disabled or desired_roles is empty.

    process_queued_jobs(app, linkedin_auth)
        Iterates over every pending job in the queue, runs the full
        analysis + apply pipeline, and sends Telegram notifications.

        Hard-pass jobs are silently dismissed.
        Jobs above auto_apply_threshold with all fields resolvable → auto-applied.
        Everything else is collected into a batch review message for the user.
"""
import json
import logging
from datetime import datetime, timezone

from telegram import Bot

from bot.db import ApplicationDB
from bot.fit import evaluate_fit, fit_summary_lines, score_breakdown
from bot.llm import analyze_job, generate_cover_letter, generate_field_answer, LLMError, tailor_resume
from bot.models import ApplicationRecord, QueuedJob, SavedSearch
from bot.profile import load_preferences
from bot.scraper import field_answer_hint
from bot.telegram_bot import _build_batch_message, PENDING_BATCH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-search seeding
# ---------------------------------------------------------------------------


async def ensure_auto_searches(db: ApplicationDB, profile: dict) -> int:
    """Create saved searches from desired_roles if auto_search is enabled.

    Checks the existing saved_searches table to avoid duplicates — a search
    for a given query is only inserted once.  Returns the count of newly
    created searches.

    Args:
        db: Initialised ApplicationDB.
        profile: The user's profile.yaml dict.

    Returns:
        Number of new searches created.
    """
    prefs = load_preferences(profile)

    if not prefs.auto_search:
        return 0
    if not prefs.desired_roles:
        return 0

    existing = await db.get_all_searches()
    existing_queries = {s.query.lower() for s in existing}

    location = profile.get("location", "")
    created = 0

    for role in prefs.desired_roles[:5]:  # cap at 5 auto-searches
        if role.lower() in existing_queries:
            continue
        search = SavedSearch(query=role, location=location, site="linkedin", active=True)
        await db.insert_search(search)
        existing_queries.add(role.lower())
        created += 1
        logger.info("auto_search: created saved search for %r in %r", role, location)

    return created


# ---------------------------------------------------------------------------
# Queue processing — the autonomous apply pipeline
# ---------------------------------------------------------------------------


async def process_queued_jobs(app, linkedin_auth: str) -> None:
    """Process pending jobs in the queue: analyze, auto-apply or route to review.

    For each pending job:
    - Hard-pass jobs → dismissed silently
    - Jobs above auto_apply_threshold with all LLM-resolvable fields → auto-applied
    - Jobs below threshold or with required user-input fields → left for batch review

    Sends Telegram notifications for every auto-apply (success or failure).
    At the end, if any jobs remain in the queue, sends a single batch review
    message so the user can pick up where automation left off.

    Args:
        app: The running PTB Application instance.
        linkedin_auth: Path to LinkedIn auth state JSON file.
    """
    db: ApplicationDB = app.bot_data["db"]
    profile: dict = app.bot_data["profile"]
    registry = app.bot_data["registry"]
    chat_id: int = app.bot_data["authorized_user_id"]
    bot: Bot = app.bot
    screenshot_dir: str = app.bot_data.get("screenshot_dir", "data/screenshots")

    prefs = load_preferences(profile)

    # Hard daily cap — safety rail against runaway auto-apply
    DAILY_CAP = 500
    today_iso = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
    today_stats = await db.get_stats(since_iso=today_iso)
    applied_today = today_stats.get("applied", 0)
    if applied_today >= DAILY_CAP:
        logger.info("auto_apply: daily cap of %d reached (%d applied today) — stopping", DAILY_CAP, applied_today)
        return

    pending = await db.get_pending_queue()
    pending = pending[:5]  # process at most 5 per cycle
    if not pending:
        return

    logger.info("auto_apply: processing %d queued jobs", len(pending))

    needs_review: list[QueuedJob] = []

    for queued_job in pending:
        # Skip if we've already applied via a direct URL send
        if await db.is_already_applied(queued_job.url):
            await db.update_queued_job_status(queued_job.id, "dismissed")
            continue

        adapter = registry.get(queued_job.url)
        if not adapter:
            logger.warning("auto_apply: no adapter for %s — skipping", queued_job.url)
            await db.update_queued_job_status(queued_job.id, "dismissed")
            continue

        # ----- Step 1: Fetch -----
        try:
            job_info = await adapter.fetch_job_info(queued_job.url)
        except Exception as e:
            logger.error("auto_apply: fetch failed for %s: %s", queued_job.url, e)
            needs_review.append(queued_job)
            continue

        # ----- Step 2: Analyze -----
        try:
            job_analysis = await analyze_job(job_info.raw_html, profile)
        except LLMError as e:
            logger.error("auto_apply: analyze failed for %s: %s", queued_job.url, e)
            needs_review.append(queued_job)
            continue

        # ----- Step 3: Fit check -----
        fit = evaluate_fit(job_analysis, prefs)

        if fit.hard_pass:
            await db.update_queued_job_status(queued_job.id, "dismissed")
            logger.info(
                "auto_apply: hard pass on %s — %s", queued_job.url, fit.hard_pass_reason
            )
            # Record the skip so /history shows it
            await db.insert_application(ApplicationRecord(
                url=queued_job.url,
                title=job_info.title,
                company=job_info.company,
                site=adapter.name,
                status="skipped",
                notes=f"Auto-skipped: {fit.hard_pass_reason}",
            ))
            continue

        # ----- Step 4: Decide — auto-apply or hand off to review -----
        threshold = prefs.auto_apply_threshold
        if threshold == 0 or not fit.auto_apply:
            # Auto-apply disabled or score too low — queue for manual review
            needs_review.append(queued_job)
            continue

        # ----- Step 5: Generate materials -----
        tailored_resume = ""
        cover_letter = ""
        try:
            tailored_resume = await tailor_resume(job_analysis, profile)
        except LLMError as e:
            logger.warning("auto_apply: tailor_resume failed: %s", e)
        try:
            cover_letter = await generate_cover_letter(job_analysis, profile)
        except LLMError as e:
            logger.warning("auto_apply: generate_cover_letter failed: %s", e)

        # ----- Step 6: Fill fields -----
        try:
            fields = await adapter.extract_fields(queued_job.url)
        except Exception as e:
            logger.error("auto_apply: extract_fields failed for %s: %s", queued_job.url, e)
            needs_review.append(queued_job)
            continue

        has_blocking_gap = False
        for form_field in fields:
            if form_field.field_type == "file":
                form_field.answer = profile.get("resume_path", "")
                continue
            try:
                hint = field_answer_hint(form_field)
                answer = await generate_field_answer(
                    form_field.label, f"Job: {job_info.title}", profile, job_analysis,
                    field_hint=hint,
                )
                if answer.startswith("NEEDS_USER_INPUT:"):
                    if form_field.required:
                        has_blocking_gap = True
                        break
                    # Non-required unknown field — leave blank
                else:
                    form_field.answer = answer
            except LLMError as llm_err:
                logger.warning(
                    "auto_apply: LLM error on field %r for %s: %s",
                    form_field.label, queued_job.url, llm_err,
                )
                if form_field.required:
                    has_blocking_gap = True
                    break

        if has_blocking_gap:
            # Can't submit without user input — route to review
            logger.info(
                "auto_apply: %s has required fields needing user input — routing to review",
                queued_job.url,
            )
            needs_review.append(queued_job)
            continue

        # Fill cover letter field if present
        cover_field = next(
            (f for f in fields if "cover" in f.label.lower()), None
        )
        if cover_field and not cover_field.answer and cover_letter:
            cover_field.answer = cover_letter

        # ----- Step 7: Submit -----
        resume_path = profile.get("resume_path", "")
        try:
            result = await adapter.submit_application(queued_job.url, fields, resume_path)
        except Exception as e:
            logger.error("auto_apply: submit failed for %s: %s", queued_job.url, e)
            await db.update_queued_job_status(queued_job.id, "dismissed")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ Auto-apply failed: {job_info.title} at {job_info.company}\n"
                        f"Error: {e}"
                    ),
                )
            except Exception:
                pass
            continue

        # Short-circuit: job closed or already applied (detected by adapter)
        if result.closed:
            await db.update_queued_job_status(queued_job.id, "dismissed")
            logger.info("auto_apply: job closed — %s", queued_job.url)
            continue

        if result.already_applied:
            await db.update_queued_job_status(queued_job.id, "dismissed")
            logger.info("auto_apply: already applied — %s", queued_job.url)
            continue

        # ----- Step 8: Record -----
        submitted_json = json.dumps(result.submitted_fields)
        record = ApplicationRecord(
            url=queued_job.url,
            title=job_info.title,
            company=job_info.company,
            site=adapter.name,
            status="applied" if result.success else "failed",
            submitted_fields=submitted_json,
            screenshot_path=result.screenshot_path,
            applied_at=datetime.now(timezone.utc).isoformat() if result.success else None,
            notes=result.error or "",
            cover_letter=cover_letter,
            tailored_resume=tailored_resume,
        )
        app_id = await db.insert_application(record)
        await db.update_queued_job_status(queued_job.id, "applied" if result.success else "dismissed")

        # ----- Step 9: Notify -----
        fit_lines = fit_summary_lines(job_analysis, fit, prefs)
        fit_summary = ("\n" + "\n".join(fit_lines)) if fit_lines else ""

        if result.success:
            confirmed = " ✅ confirmed" if result.submission_confirmed else " (unconfirmed)"
            breakdown = score_breakdown(job_analysis, prefs)
            msg = (
                f"🤖 *Auto-applied*: {job_info.title} at {job_info.company}{confirmed}\n\n"
                f"{breakdown}"
                f"{fit_summary}"
            )
            extras = []
            if tailored_resume:
                extras.append(f"/resume {app_id}")
            if cover_letter:
                extras.append(f"/coverletter {app_id}")
            if extras:
                msg += "\n" + " | ".join(extras)
            if result.missing_fields:
                msg += f"\n⚠ Unverified fields: {', '.join(result.missing_fields)}"
        else:
            msg = (
                f"❌ *Auto-apply failed*: {job_info.title} at {job_info.company}\n"
                f"{result.error}"
            )

        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            # Send screenshot if available
            if result.success and result.screenshot_path:
                try:
                    with open(result.screenshot_path, "rb") as f:
                        await bot.send_photo(chat_id=chat_id, photo=f)
                except Exception as photo_err:
                    logger.warning("auto_apply: could not send screenshot: %s", photo_err)
        except Exception as e:
            logger.error("auto_apply: notification failed: %s", e)

    # ----- Send batch review for jobs that didn't auto-apply -----
    if needs_review:
        review_jobs = await db.get_pending_queue()  # refresh — only still-pending ones
        if review_jobs:
            try:
                msg = _build_batch_message(review_jobs)
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                app.bot_data[PENDING_BATCH] = review_jobs
            except Exception as e:
                logger.error("auto_apply: batch review message failed: %s", e)
