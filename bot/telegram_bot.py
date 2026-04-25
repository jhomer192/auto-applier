import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.adapters import AdapterRegistry
from bot.db import ApplicationDB
from bot.fit import evaluate_fit, fit_summary_lines
from bot.inbox import classify_email, GmailInbox
from bot.llm import analyze_job, claude_call, extract_achievements, generate_cover_letter, generate_field_answer, LLMError, tailor_resume
from bot.models import ApplicationRecord, EmailThread, FitReport, JobPreferences, PendingJob, QueuedJob, SavedSearch
from bot.profile import load_preferences, save_preferences
from bot.ratelimit import enforce_rate_limit, RateLimitExceeded
from bot.scraper import field_answer_hint

logger = logging.getLogger(__name__)

# Conversation state keys stored in context.user_data
PENDING_JOB = "pending_job"
AWAITING_FIELD = "awaiting_field"
AWAITING_EMAIL_REPLY = "awaiting_email_reply"  # value: EmailThread

# Profile interview state
PROFILE_INTERVIEW = "profile_interview"   # value: {"step": int, "answers": [(q, a), ...]}

# Passive job discovery batch state (bot_data — set by background task)
PENDING_BATCH = "pending_batch"   # list[QueuedJob] shown to user awaiting number reply
BATCH_QUEUE = "batch_queue"       # list[QueuedJob] selected by user, awaiting sequential processing

# Profile interview questions
_PROFILE_QUESTIONS = [
    "What's your biggest career win so far? Walk me through what you did and what happened — metrics are gold (numbers, percentages, scale).",
    "Any notable technical accomplishments? Systems you built or scaled, hard bugs you solved, tools others adopted?",
    "What would a past manager say is your standout quality? Any specific situations that back that up?",
    "Anything else worth capturing — side projects with traction, open source contributions, awards, things you're proud of that aren't on your resume yet?",
]

SUPPORTED_PATTERNS = [
    r"linkedin\.com/jobs/view/\d+",
    r"boards\.greenhouse\.io/.+",
    r"jobs\.lever\.co/.+",
]


def _is_job_url(text: str) -> bool:
    return any(re.search(p, text) for p in SUPPORTED_PATTERNS)


class AutoApplierBot:
    def __init__(
        self,
        token: str,
        chat_id: int,
        db: ApplicationDB,
        profile: dict,
        registry: AdapterRegistry,
        screenshot_dir: str = "data/screenshots",
        gmail_inbox: GmailInbox | None = None,
        profile_path: str = "profile.yaml",
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._profile = profile
        self._registry = registry
        self._screenshot_dir = screenshot_dir
        self._gmail_inbox = gmail_inbox
        self._profile_path = profile_path

    def build_app(self, post_init=None) -> Application:
        builder = Application.builder().token(self._token)
        if post_init is not None:
            builder = builder.post_init(post_init)
        app = builder.build()

        # Store refs in bot_data for handler access
        app.bot_data["db"] = self._db
        app.bot_data["profile"] = self._profile
        app.bot_data["registry"] = self._registry
        app.bot_data["authorized_user_id"] = self._chat_id
        app.bot_data["screenshot_dir"] = self._screenshot_dir
        app.bot_data["gmail_inbox"] = self._gmail_inbox
        app.bot_data["profile_path"] = self._profile_path

        app.add_handler(CommandHandler("start", cmd_help))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("history", cmd_history))
        app.add_handler(CommandHandler("cancel", cmd_cancel))
        app.add_handler(CommandHandler("search", cmd_search))
        app.add_handler(CommandHandler("resume", cmd_resume))
        app.add_handler(CommandHandler("coverletter", cmd_coverletter))
        app.add_handler(CommandHandler("profile", cmd_profile))
        app.add_handler(CommandHandler("prefs", cmd_prefs))
        app.add_handler(CommandHandler("queue", cmd_queue))
        app.add_handler(CommandHandler("report", cmd_report))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        return app

    def run(self) -> None:
        app = self.build_app()
        logger.info("Bot starting...")
        app.run_polling(drop_pending_updates=True)


def _auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True only if message is from the authorized chat."""
    return bool(update.effective_user and update.effective_user.id == context.bot_data["authorized_user_id"])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return
    await update.message.reply_text(
        "Auto Job Applier\n\n"
        "Send a job URL to apply:\n"
        "  \u2022 linkedin.com/jobs/view/...\n"
        "  \u2022 boards.greenhouse.io/...\n"
        "  \u2022 jobs.lever.co/...\n\n"
        "Application commands:\n"
        "/status \u2014 application counts\n"
        "/history [N] \u2014 last N applications (default 10)\n"
        "/resume <id> \u2014 tailored resume for application\n"
        "/coverletter <id> \u2014 cover letter for application\n"
        "/cancel \u2014 cancel pending item\n\n"
        "Passive discovery commands:\n"
        "/queue \u2014 show pending discovered jobs (reply with numbers to investigate)\n"
        "/report \u2014 application stats (today/week/all-time) + pipeline summary\n\n"
        "Job search commands:\n"
        "/search add <query> [location] \u2014 save a search\n"
        "/search list \u2014 list saved searches\n"
        "/search rm <id> \u2014 remove a search\n\n"
        "Profile commands:\n"
        "/profile \u2014 add achievements to your profile\n"
        "/prefs \u2014 view/set job preferences (salary, roles, auto-apply)\n\n"
        "/help \u2014 this message"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]
    recent = await db.get_recent(limit=100)
    counts: dict[str, int] = {}
    for r in recent:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines = ["Application Summary:"]
    for status, count in sorted(counts.items()):
        lines.append(f"  {status}: {count}")

    if recent:
        lines.append("\nMost Recent:")
        for r in recent[:3]:
            lines.append(f"  {r.title} @ {r.company} \u2014 {r.status}")
    else:
        lines.append("\nNo applications yet.")

    await update.message.reply_text("\n".join(lines))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]

    # Parse optional N arg
    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            pass

    records = await db.get_recent(limit=limit)
    if not records:
        await update.message.reply_text("No applications yet.")
        return

    lines = [f"Last {len(records)} applications:"]
    for r in records:
        date = r.applied_at[:10] if r.applied_at else r.created_at[:10]
        lines.append(f"[{r.id}] {r.title} @ {r.company} \u2014 {r.status} \u2014 {date}")

    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return

    if PENDING_JOB in context.user_data:
        pending: PendingJob = context.user_data.pop(PENDING_JOB)
        context.user_data.pop(AWAITING_FIELD, None)
        title = pending.job_info.title
        company = pending.job_info.company
        await update.message.reply_text(f"Cancelled pending application for {title} at {company}.")

    elif AWAITING_FIELD in context.user_data:
        context.user_data.pop(AWAITING_FIELD, None)
        await update.message.reply_text("Cancelled.")

    elif context.bot_data.get(AWAITING_EMAIL_REPLY):
        queue: list = context.bot_data[AWAITING_EMAIL_REPLY]
        queue.pop(0)
        if not queue:
            context.bot_data.pop(AWAITING_EMAIL_REPLY, None)
            await update.message.reply_text("Dismissed.")
        else:
            next_thread: EmailThread = queue[0]
            next_category = classify_email(next_thread)
            preview = next_thread.body_preview.strip()[:300]
            if next_category == "offer":
                prompt_line = "Tell me how you'd like to respond \u2014 accept, decline, or counter."
                header = "\U0001f389 *Job offer*"
            else:
                prompt_line = "Share your availability and I'll write the reply."
                header = "\U0001f4e8 *Interview request*"
            await update.message.reply_text(
                f"{header}\n\n"
                f"*From:* {next_thread.from_address}\n"
                f"*Subject:* {next_thread.subject}\n\n"
                f"{preview}\n\n"
                f"{prompt_line} Or /cancel to ignore.",
                parse_mode="Markdown",
            )

    elif context.user_data.get(PROFILE_INTERVIEW):
        context.user_data.pop(PROFILE_INTERVIEW, None)
        await update.message.reply_text("Profile interview cancelled. Nothing was saved.")

    else:
        await update.message.reply_text("Nothing to cancel.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return

    text = update.message.text.strip()
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]
    registry: AdapterRegistry = context.bot_data["registry"]

    # Case 1a: waiting for a recruiter email reply from the user
    # (stored as a queue in bot_data because background tasks can't access user_data)
    if context.bot_data.get(AWAITING_EMAIL_REPLY):
        await _handle_email_reply(update, context, text)
        return

    # Case 1b: profile interview in progress
    if context.user_data.get(PROFILE_INTERVIEW):
        await _handle_profile_answer(update, context, text)
        return

    # Case 1c: we are waiting for a field answer from the user
    if context.user_data.get(AWAITING_FIELD):
        await _handle_field_answer(update, context, text)
        return

    # Case 2: Y/N response to a pending job
    pending: PendingJob | None = context.user_data.get(PENDING_JOB)
    if pending:
        if text.lower() in ("y", "yes"):
            await _proceed_with_application(update, context, pending)
        elif text.lower() in ("n", "no"):
            adapter = registry.get(pending.url)
            site = adapter.name if adapter else "unknown"
            await db.insert_application(ApplicationRecord(
                url=pending.url,
                title=pending.job_info.title,
                company=pending.job_info.company,
                site=site,
                status="skipped",
            ))
            context.user_data.pop(PENDING_JOB, None)
            await update.message.reply_text("Skipped.")
            await _maybe_process_next_batch_item(update, context)
        else:
            await update.message.reply_text("Reply Y to apply or N to skip. Or /cancel.")
        return

    # Case 2b: batch selection response (from /queue or passive discovery message)
    if context.bot_data.get(PENDING_BATCH) is not None:
        await _handle_batch_response(update, context, text)
        return

    # Case 3: new job URL
    if _is_job_url(text):
        await _handle_job_url(update, context, text)
        return

    # Case 4: unrecognized
    await update.message.reply_text(
        "Send me a job URL (LinkedIn Easy Apply, Greenhouse, or Lever) to get started.\n/help for commands."
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage saved job searches.

    /search add <query> [location] — save a new periodic search
    /search list                   — show all saved searches
    /search rm <id>                — deactivate a search
    """
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/search add <query> [location] — e.g. /search add 'ML Engineer' 'San Francisco, CA'\n"
            "/search list\n"
            "/search rm <id>"
        )
        return

    sub = args[0].lower()

    if sub == "add":
        if len(args) < 2:
            await update.message.reply_text("Usage: /search add <query> [location]")
            return
        query = args[1]
        location = " ".join(args[2:]) if len(args) > 2 else ""
        search = SavedSearch(query=query, location=location)
        search_id = await db.insert_search(search)
        loc_str = f" in {location}" if location else ""
        await update.message.reply_text(
            f"Search saved (ID {search_id}): \"{query}\"{loc_str}\n"
            "I'll check every 30 minutes and send you new matches."
        )

    elif sub == "list":
        searches = await db.get_all_searches()
        if not searches:
            await update.message.reply_text("No saved searches.")
            return
        lines = ["Saved searches:"]
        for s in searches:
            status = "active" if s.active else "paused"
            loc = f" | {s.location}" if s.location else ""
            last = s.last_checked[:16].replace("T", " ") if s.last_checked else "never"
            lines.append(f"[{s.id}] {s.query}{loc} — {status} — last checked: {last}")
        await update.message.reply_text("\n".join(lines))

    elif sub == "rm":
        if len(args) < 2:
            await update.message.reply_text("Usage: /search rm <id>")
            return
        try:
            search_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Search ID must be a number.")
            return
        await db.deactivate_search(search_id)
        await update.message.reply_text(f"Search {search_id} deactivated.")

    else:
        await update.message.reply_text("Unknown subcommand. Use: add, list, or rm.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the tailored resume for a past application.

    /resume <id>
    """
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /resume <application id>")
        return
    try:
        app_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    record = await db.get_by_id(app_id)
    if not record:
        await update.message.reply_text(f"No application with ID {app_id}.")
        return
    if not record.tailored_resume:
        await update.message.reply_text(
            f"No tailored resume stored for application {app_id} "
            f"({record.title} @ {record.company}).\n"
            "Resume tailoring is generated for new applications going forward."
        )
        return
    header = f"Tailored resume for [{app_id}] {record.title} @ {record.company}:\n\n"
    await update.message.reply_text(header + record.tailored_resume)


async def cmd_coverletter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the cover letter for a past application.

    /coverletter <id>
    """
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /coverletter <application id>")
        return
    try:
        app_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    record = await db.get_by_id(app_id)
    if not record:
        await update.message.reply_text(f"No application with ID {app_id}.")
        return
    if not record.cover_letter:
        await update.message.reply_text(
            f"No cover letter stored for application {app_id} "
            f"({record.title} @ {record.company}).\n"
            "Cover letters are generated for new applications going forward."
        )
        return
    header = f"Cover letter for [{app_id}] {record.title} @ {record.company}:\n\n"
    await update.message.reply_text(header + record.cover_letter)


async def cmd_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View and update job preferences.

    /prefs                       — show current preferences
    /prefs roles <r1,r2,...>     — set desired role types
    /prefs salary <min> [target] — set salary floor and target (annual USD)
    /prefs seniority <s1,s2,...> — set acceptable seniority levels
    /prefs arrangement <r,h,o>   — set work arrangement preference
    /prefs autoapply <0-100>     — set auto-apply threshold (0 = disabled)
    /prefs exclude <company>     — add company to exclusion list
    /prefs unexclude <company>   — remove from exclusion list
    /prefs sponsorship yes|no    — toggle visa sponsorship requirement
    """
    if not _auth(update, context):
        return

    profile: dict = context.bot_data["profile"]
    profile_path: str = context.bot_data["profile_path"]
    prefs = load_preferences(profile)
    args = context.args or []

    if not args:
        # Show current preferences
        roles = ", ".join(prefs.desired_roles) if prefs.desired_roles else "any"
        min_s = f"${prefs.min_salary:,}" if prefs.min_salary else "not set"
        target_s = f"${prefs.target_salary:,}" if prefs.target_salary else "not set"
        seniority = ", ".join(prefs.seniority) if prefs.seniority else "any"
        arrangement = ", ".join(prefs.work_arrangement) if prefs.work_arrangement else "any"
        excluded = ", ".join(prefs.excluded_companies) if prefs.excluded_companies else "none"
        auto = f"score >= {prefs.auto_apply_threshold}" if prefs.auto_apply_threshold else "off"
        gap = f"{prefs.min_apply_gap_minutes}–{prefs.max_apply_gap_minutes} min"
        cap = str(prefs.max_applies_per_day) if prefs.max_applies_per_day else "30 (default)"
        sponsorship = "yes (need sponsorship)" if prefs.requires_sponsorship else "no"
        await update.message.reply_text(
            "Current job preferences:\n\n"
            f"Roles: {roles}\n"
            f"Min salary: {min_s}\n"
            f"Target salary: {target_s}\n"
            f"Seniority: {seniority}\n"
            f"Arrangement: {arrangement}\n"
            f"Excluded companies: {excluded}\n"
            f"Auto-apply: {auto}\n"
            f"Visa sponsorship needed: {sponsorship}\n\n"
            "Rate limiting:\n"
            f"  Apply gap: {gap} (randomised)\n"
            f"  Daily cap: {cap} applications/day\n\n"
            "Update with:\n"
            "/prefs roles Backend Engineer,Staff Engineer\n"
            "/prefs salary 180000 220000\n"
            "/prefs seniority senior,staff,principal\n"
            "/prefs arrangement remote,hybrid\n"
            "/prefs autoapply 85\n"
            "/prefs exclude Meta\n"
            "/prefs unexclude Meta\n"
            "/prefs pace 3 10    (min–max gap in minutes)\n"
            "/prefs dailycap 20  (max applications per day)\n"
            "/prefs sponsorship yes|no"
        )
        return

    sub = args[0].lower()

    if sub == "roles":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs roles <role1,role2,...>")
            return
        raw = " ".join(args[1:])
        prefs.desired_roles = [r.strip().lower() for r in raw.split(",") if r.strip()]
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Desired roles set: {', '.join(prefs.desired_roles)}")

    elif sub == "salary":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs salary <min> [target]")
            return
        try:
            prefs.min_salary = int(args[1].replace(",", "").replace("$", ""))
            if len(args) > 2:
                prefs.target_salary = int(args[2].replace(",", "").replace("$", ""))
        except ValueError:
            await update.message.reply_text("Salary must be a number (e.g. 180000).")
            return
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        msg = f"Salary floor set to ${prefs.min_salary:,}"
        if prefs.target_salary:
            msg += f", target ${prefs.target_salary:,}"
        await update.message.reply_text(msg)

    elif sub == "seniority":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /prefs seniority <level1,level2,...>\n"
                "Levels: junior, mid, senior, staff, principal, director"
            )
            return
        raw = " ".join(args[1:])
        prefs.seniority = [s.strip().lower() for s in raw.split(",") if s.strip()]
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Seniority set: {', '.join(prefs.seniority)}")

    elif sub == "arrangement":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /prefs arrangement <remote|hybrid|onsite> (comma-separated for multiple)"
            )
            return
        raw = " ".join(args[1:])
        valid = {"remote", "hybrid", "onsite"}
        chosen = [a.strip().lower() for a in raw.split(",") if a.strip().lower() in valid]
        if not chosen:
            await update.message.reply_text(f"Valid options: {', '.join(sorted(valid))}")
            return
        prefs.work_arrangement = chosen
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Work arrangement set: {', '.join(prefs.work_arrangement)}")

    elif sub == "autoapply":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs autoapply <0-100> (0 = disabled)")
            return
        try:
            threshold = int(args[1])
            if not (0 <= threshold <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Threshold must be 0-100. Use 0 to disable.")
            return
        prefs.auto_apply_threshold = threshold
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        if threshold == 0:
            await update.message.reply_text("Auto-apply disabled. You'll always be asked.")
        else:
            await update.message.reply_text(
                f"Auto-apply enabled at score >= {threshold}.\n"
                "Jobs that hit the threshold AND pass all your filters will be submitted without asking."
            )

    elif sub == "exclude":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs exclude <company name>")
            return
        company = " ".join(args[1:])
        if company not in prefs.excluded_companies:
            prefs.excluded_companies.append(company)
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Added to exclusion list: {company}")

    elif sub == "unexclude":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs unexclude <company name>")
            return
        company = " ".join(args[1:])
        prefs.excluded_companies = [c for c in prefs.excluded_companies if c.lower() != company.lower()]
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Removed from exclusion list: {company}")

    elif sub == "pace":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /prefs pace <min_minutes> [max_minutes]\n"
                "Example: /prefs pace 3 8 — wait 3–8 minutes between applications"
            )
            return
        try:
            min_gap = int(args[1])
            max_gap = int(args[2]) if len(args) > 2 else min_gap + 4
            if min_gap < 1 or max_gap < min_gap:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Both values must be positive integers, max >= min.")
            return
        prefs.min_apply_gap_minutes = min_gap
        prefs.max_apply_gap_minutes = max_gap
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(
            f"Apply pace set: {min_gap}–{max_gap} minutes between submissions."
        )

    elif sub == "dailycap":
        if len(args) < 2:
            await update.message.reply_text("Usage: /prefs dailycap <number>")
            return
        try:
            cap = int(args[1])
            if cap < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Daily cap must be a positive integer.")
            return
        prefs.max_applies_per_day = cap
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        await update.message.reply_text(f"Daily cap set to {cap} applications/day.")

    elif sub == "sponsorship":
        if len(args) < 2 or args[1].lower() not in ("yes", "no"):
            await update.message.reply_text("Usage: /prefs sponsorship yes|no")
            return
        prefs.requires_sponsorship = args[1].lower() == "yes"
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        if prefs.requires_sponsorship:
            await update.message.reply_text(
                "Visa sponsorship requirement set to yes.\n"
                "Jobs that explicitly don't sponsor will be hard-passed. "
                "Jobs that don't mention it will get a warning."
            )
        else:
            await update.message.reply_text("Visa sponsorship requirement set to no.")

    else:
        await update.message.reply_text(
            "Unknown subcommand. Options: roles, salary, seniority, arrangement, "
            "autoapply, exclude, unexclude, pace, dailycap, sponsorship"
        )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start an achievement-mining interview to enrich the candidate profile."""
    if not _auth(update, context):
        return
    if context.user_data.get(PROFILE_INTERVIEW):
        await update.message.reply_text(
            "Profile interview already in progress. Answer the question above, or /cancel to quit."
        )
        return
    context.user_data[PROFILE_INTERVIEW] = {"step": 0, "answers": []}
    await update.message.reply_text(
        "Let's add some achievements to your profile. I'll ask 4 quick questions — "
        "be as specific as you can. /cancel at any time to quit.\n\n"
        f"*Question 1 of {len(_PROFILE_QUESTIONS)}:*\n{_PROFILE_QUESTIONS[0]}",
        parse_mode="Markdown",
    )


async def _handle_profile_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Process one answer in the profile interview flow."""
    state: dict = context.user_data[PROFILE_INTERVIEW]
    step: int = state["step"]
    answers: list = state["answers"]

    answers.append((_PROFILE_QUESTIONS[step], text))
    step += 1
    state["step"] = step
    state["answers"] = answers

    if step < len(_PROFILE_QUESTIONS):
        await update.message.reply_text(
            f"*Question {step + 1} of {len(_PROFILE_QUESTIONS)}:*\n{_PROFILE_QUESTIONS[step]}",
            parse_mode="Markdown",
        )
        return

    # All questions answered — extract achievements
    context.user_data.pop(PROFILE_INTERVIEW, None)
    await update.message.reply_text("Extracting achievements from your answers...")

    profile: dict = context.bot_data["profile"]
    profile_path: str = context.bot_data["profile_path"]

    try:
        new_yaml = await extract_achievements(answers, profile)
    except LLMError as e:
        await update.message.reply_text(f"Could not extract achievements: {e}")
        return

    # Parse the returned YAML
    try:
        parsed = yaml.safe_load(new_yaml)
        new_achievements = parsed.get("achievements", []) if isinstance(parsed, dict) else []
    except yaml.YAMLError as e:
        await update.message.reply_text(f"Could not parse extracted achievements: {e}\n\nRaw output:\n{new_yaml}")
        return

    if not new_achievements:
        await update.message.reply_text(
            "No new achievements could be extracted from your answers. "
            "Nothing was saved. Try being more specific — include what you built, "
            "the outcome, and any numbers."
        )
        return

    # Show diff before writing
    lines = ["Extracted achievements:"]
    for ach in new_achievements:
        lines.append(f"\n• {ach.get('summary', '')}")
        if ach.get("impact"):
            lines.append(f"  Impact: {ach['impact']}")
        if ach.get("skills"):
            lines.append(f"  Skills: {', '.join(ach['skills'])}")
    lines.append("\nSaving to profile.yaml...")
    await update.message.reply_text("\n".join(lines))

    # Merge into profile.yaml
    try:
        with open(profile_path) as f:
            current = yaml.safe_load(f) or {}
        existing = current.get("achievements", [])
        if not isinstance(existing, list):
            existing = []
        current["achievements"] = existing + new_achievements
        with open(profile_path, "w") as f:
            yaml.dump(current, f, default_flow_style=False, allow_unicode=True)
        # Reload the live profile reference
        context.bot_data["profile"] = current
        await update.message.reply_text(
            f"Profile updated — {len(new_achievements)} achievement(s) added."
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to save profile: {e}")


async def _handle_job_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Handle a job URL: fetch, analyze, evaluate fit, generate materials, then show informed Y/N."""
    registry: AdapterRegistry = context.bot_data["registry"]
    profile: dict = context.bot_data["profile"]
    db: ApplicationDB = context.bot_data["db"]
    adapter = registry.get(url)

    if not adapter:
        await update.message.reply_text(
            "Unsupported site. I can apply on:\n"
            "\u2022 linkedin.com/jobs/view/...\n"
            "\u2022 boards.greenhouse.io/...\n"
            "\u2022 jobs.lever.co/..."
        )
        return

    await update.message.reply_text("Fetching and analyzing job...")

    try:
        job_info = await adapter.fetch_job_info(url)
    except Exception as e:
        await update.message.reply_text(f"Could not fetch that URL: {e}")
        return

    # Run full analysis immediately — user sees informed Y/N, not a blind one
    try:
        job_analysis = await analyze_job(job_info.raw_html, profile)
    except LLMError as e:
        await update.message.reply_text(f"Analysis failed: {e}")
        return

    # Evaluate fit against preferences
    prefs = load_preferences(profile)
    fit = evaluate_fit(job_analysis, prefs)

    # Hard pass — don't bother the user with Y/N
    if fit.hard_pass:
        await db.insert_application(ApplicationRecord(
            url=url,
            title=job_info.title,
            company=job_info.company,
            site=adapter.name,
            status="skipped",
            notes=f"Auto-skipped: {fit.hard_pass_reason}",
        ))
        await update.message.reply_text(f"Auto-skipped: {fit.hard_pass_reason}")
        return

    # Generate tailored materials
    tailored_resume_text = ""
    cover_letter_text = ""
    try:
        tailored_resume_text = await tailor_resume(job_analysis, profile)
    except LLMError as e:
        logger.warning("tailor_resume failed: %s", e)
    try:
        cover_letter_text = await generate_cover_letter(job_analysis, profile)
    except LLMError as e:
        logger.warning("generate_cover_letter failed: %s", e)

    # Pre-fill form fields
    fields = await adapter.extract_fields(url)
    needs_user_input: list[int] = []
    for i, form_field in enumerate(fields):
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
                needs_user_input.append(i)
            else:
                form_field.answer = answer
        except LLMError:
            needs_user_input.append(i)

    cover_field_idx = next(
        (i for i, f in enumerate(fields) if "cover" in f.label.lower()), None
    )
    if cover_field_idx is not None and fields[cover_field_idx].answer == "" and cover_letter_text:
        fields[cover_field_idx].answer = cover_letter_text
        if cover_field_idx in needs_user_input:
            needs_user_input.remove(cover_field_idx)

    pending = PendingJob(
        url=url,
        job_info=job_info,
        fields=fields,
        cover_letter=cover_letter_text,
        tailored_resume=tailored_resume_text,
        fit_report=fit,
    )
    if needs_user_input:
        pending.awaiting_fields = [fields[i] for i in needs_user_input]

    # Auto-apply: threshold met, all fit checks pass, no fields needing user input
    if fit.auto_apply and not needs_user_input:
        fit_lines = fit_summary_lines(job_analysis, fit, prefs)
        summary = "\n".join(fit_lines)
        await update.message.reply_text(
            f"Auto-applying to *{job_info.title}* at *{job_info.company}* "
            f"(score {job_analysis.match_score}/100 \u2265 threshold {prefs.auto_apply_threshold})\n"
            + (f"{summary}\n" if summary else ""),
            parse_mode="Markdown",
        )
        await _submit_application(update, context, pending)
        return

    # Build informed Y/N prompt
    fit_lines = fit_summary_lines(job_analysis, fit, prefs)
    fit_block = ("\n" + "\n".join(fit_lines)) if fit_lines else ""
    cover_preview = ""
    if cover_letter_text:
        cover_preview = "\n\nCover letter preview:\n" + cover_letter_text[:300] + (
            "..." if len(cover_letter_text) > 300 else ""
        )
    manual_note = ""
    if needs_user_input:
        manual_note = f"\n\n({len(needs_user_input)} field(s) need your input after Y)"

    context.user_data[PENDING_JOB] = pending
    await update.message.reply_text(
        f"*{job_info.title}* at *{job_info.company}*\n"
        f"Match: *{job_analysis.match_score}/100*"
        f"{fit_block}"
        f"{cover_preview}"
        f"{manual_note}\n\n"
        "Apply? Y / N",
        parse_mode="Markdown",
    )


async def _proceed_with_application(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingJob,
) -> None:
    """User confirmed Y. Materials are already generated. Start field prompts or submit."""
    if pending.awaiting_fields:
        pending.current_field_index = 0
        context.user_data[PENDING_JOB] = pending
        context.user_data[AWAITING_FIELD] = True
        form_field = pending.awaiting_fields[0]
        await update.message.reply_text(
            f"I need a few answers before I can apply.\n\n"
            f"*{form_field.label}*: This field is required but is not in your profile.\n\n"
            "Please answer:",
            parse_mode="Markdown",
        )
        return

    # All fields resolved — submit directly
    await _submit_application(update, context, pending)


async def _handle_field_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    pending: PendingJob = context.user_data[PENDING_JOB]

    # Store answer
    current_field = pending.awaiting_fields[pending.current_field_index]
    current_field.answer = text

    # Find and update in main fields list
    for f in pending.fields:
        if f.label == current_field.label and f.answer == "":
            f.answer = text
            break

    pending.current_field_index += 1

    if pending.current_field_index < len(pending.awaiting_fields):
        # Ask next field
        next_field = pending.awaiting_fields[pending.current_field_index]
        await update.message.reply_text(
            f"*{next_field.label}*: Please answer:",
            parse_mode="Markdown",
        )
        return

    # All user-provided fields answered — submit
    context.user_data.pop(AWAITING_FIELD, None)
    await _submit_application(update, context, pending)


async def _handle_email_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Send user's reply to the recruiter, composing a professional scheduling
    email via Claude when the thread is an interview or offer."""
    queue: list = context.bot_data.get(AWAITING_EMAIL_REPLY, [])
    if not queue:
        return
    thread: EmailThread = queue.pop(0)
    if not queue:
        context.bot_data.pop(AWAITING_EMAIL_REPLY, None)
    inbox: GmailInbox | None = context.bot_data.get("gmail_inbox")
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]

    if not inbox:
        await update.message.reply_text("Gmail not configured — reply not sent.")
        return

    # For interview/offer threads, use Claude to compose a professional reply
    body = text
    category = classify_email(thread)
    if category in ("interview", "offer"):
        await update.message.reply_text("Composing reply...")
        try:
            if category == "offer":
                body = await _compose_offer_reply(thread, text, profile)
            else:
                body = await _compose_scheduling_reply(thread, text, profile)
        except LLMError as e:
            logger.warning("Could not compose reply: %s — sending raw text", e)
            body = text

    try:
        await inbox.send_reply(thread, body)
        await db.insert_outbound_email(
            thread_id=thread.thread_id,
            to_address=thread.from_address,
            subject=thread.subject,
            body=body,
        )
        # Echo what was sent so the user knows exactly what went out
        await update.message.reply_text(f"Sent to {thread.from_address}:\n\n{body}")
    except Exception as e:
        logger.error("Failed to send email reply: %s", e)
        await update.message.reply_text(f"Failed to send reply: {e}")

    # If more emails are queued, prompt for the next one now
    remaining: list = context.bot_data.get(AWAITING_EMAIL_REPLY, [])
    if remaining:
        next_thread = remaining[0]
        next_category = classify_email(next_thread)
        preview = next_thread.body_preview.strip()[:300]
        if next_category == "offer":
            prompt_line = "Tell me how you'd like to respond \u2014 accept, decline, or counter."
            header = "\U0001f389 *Job offer*"
        else:
            prompt_line = "Share your availability and I'll write the reply."
            header = "\U0001f4e8 *Interview request*"
        await update.message.reply_text(
            f"{header}\n\n"
            f"*From:* {next_thread.from_address}\n"
            f"*Subject:* {next_thread.subject}\n\n"
            f"{preview}\n\n"
            f"{prompt_line} Or /cancel to ignore.",
            parse_mode="Markdown",
        )


_EMAIL_INJECTION_MARKERS = ["CONSTRAINT:", "NEEDS_USER_INPUT", "PROFILE:", "FORM FIELD:"]


def _sanitize_email_text(text: str) -> str:
    """Strip prompt-injection markers from recruiter email content before it
    enters an LLM prompt. Mirrors the sentinel check in llm._sanitize_profile."""
    for marker in _EMAIL_INJECTION_MARKERS:
        text = text.replace(marker, "[REDACTED]")
    return text


async def _compose_offer_reply(
    thread: EmailThread,
    user_input: str,
    profile: dict,
) -> str:
    """Use Claude CLI to write a professional offer response.

    user_input is the user's intent: accept / decline / counter with details.
    Includes salary context (target_salary from preferences) when countering.
    """
    name = profile.get("name", "")
    safe_body = _sanitize_email_text(thread.body_preview)

    # Include salary context so Claude can reference target numbers when countering
    prefs = load_preferences(profile)
    salary_context = ""
    if prefs.target_salary:
        salary_context = f"CANDIDATE'S TARGET SALARY: ${prefs.target_salary:,} per year\n"
    elif prefs.min_salary:
        salary_context = f"CANDIDATE'S MINIMUM ACCEPTABLE SALARY: ${prefs.min_salary:,} per year\n"

    prompt = (
        f"Write a short, professional reply to a job offer email.\n\n"
        f"OFFER EMAIL:\n"
        f"From: {thread.from_address}\n"
        f"Subject: {thread.subject}\n"
        f"Message: {safe_body}\n\n"
        f"CANDIDATE NAME: {name}\n"
        f"{salary_context}\n"
        f"CANDIDATE'S INTENT:\n{user_input}\n\n"
        "Write ONLY the email body (no subject line, no headers).\n"
        "Keep it to 3-5 sentences. Be warm and professional.\n"
        "If accepting: express genuine enthusiasm and confirm any next steps.\n"
        "If declining: be gracious and keep the door open.\n"
        "If countering: state the counter clearly and professionally, "
        "framing it as a question rather than a demand. "
        "If a target salary is provided above, use it as the counter number "
        "unless the candidate's intent specifies a different amount.\n"
        "Use the candidate's intent exactly — do not invent details."
    )
    return await claude_call(prompt, max_tokens=400)


async def _compose_scheduling_reply(
    thread: EmailThread,
    user_input: str,
    profile: dict,
) -> str:
    """Use Claude CLI to write a professional scheduling reply.

    user_input is the user's raw availability / intent (e.g. 'Tuesday 2-4pm or
    Thursday morning, prefer video call'). Claude turns it into a polished email.
    """
    name = profile.get("name", "")
    safe_body = _sanitize_email_text(thread.body_preview)
    prompt = (
        f"Write a short, professional reply to a recruiter interview request.\n\n"
        f"RECRUITER EMAIL:\n"
        f"From: {thread.from_address}\n"
        f"Subject: {thread.subject}\n"
        f"Message: {safe_body}\n\n"
        f"CANDIDATE NAME: {name}\n\n"
        f"CANDIDATE'S AVAILABILITY / INTENT:\n{user_input}\n\n"
        "Write ONLY the email body (no subject line, no 'Subject:', no headers).\n"
        "Keep it to 3-5 sentences. Be warm and professional.\n"
        "Confirm interest in the role, share the availability exactly as given, "
        "and close with a thank-you."
    )
    return await claude_call(prompt, max_tokens=400)


async def notify_new_emails(app: Application) -> None:
    """Called from the background polling loop. Checks DB for unnotified inbound
    emails and sends a Telegram message for each one, prompting the user to reply.

    The last notified thread is stored in bot_data[AWAITING_EMAIL_REPLY] so the
    next free-text message the user sends becomes the reply.
    """
    db: ApplicationDB = app.bot_data["db"]
    chat_id: int = app.bot_data["authorized_user_id"]
    bot = app.bot

    pending = await db.get_unnotified_emails()
    for email_thread in pending:
        category = classify_email(email_thread)
        logger.info(
            "inbox: email from %s classified as %r (subject: %r)",
            email_thread.from_address, category, email_thread.subject,
        )

        if category not in ("interview", "offer"):
            # Silently mark read — rejections and confirmations don't need a reply
            await db.mark_email_notified(email_thread.id)
            continue

        preview = email_thread.body_preview.strip()[:300]
        if category == "offer":
            prompt_line = (
                "Tell me how you'd like to respond \u2014 accept, decline, or negotiate "
                "(e.g. \"accept\" / \"decline\" / \"counter at $X, start date Y\") "
                "and I'll write the reply."
            )
            header = "\U0001f389 *Job offer*"
        else:
            prompt_line = (
                "Share your availability and I'll write the reply \u2014 e.g. "
                "\"Tuesday 2\u20135pm or Thursday morning, prefer video call\"."
            )
            header = "\U0001f4e8 *Interview request*"

        text = (
            f"{header}\n\n"
            f"*From:* {email_thread.from_address}\n"
            f"*Subject:* {email_thread.subject}\n\n"
            f"{preview}\n\n"
            f"{prompt_line}\n"
            f"Or type a full reply. /cancel to ignore."
        )
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            await db.mark_email_notified(email_thread.id)
            # Append to queue so multiple emails in one poll are all handled
            queue: list = app.bot_data.setdefault(AWAITING_EMAIL_REPLY, [])
            queue.append(email_thread)
        except Exception as e:
            logger.error("notify_new_emails: failed to send notification: %s", e)


async def notify_search_matches(app: Application, linkedin_auth: str) -> None:
    """Check saved searches for new job matches and queue them for batch review.

    Called from the background search poll loop. For each active search, runs the
    LinkedIn scraper, filters out already-seen URLs, and adds new matches to the
    job_queue table. At the end, if any new jobs were queued, sends a single batch
    message to Telegram so the user can select which to investigate.

    Args:
        app: The running PTB Application instance.
        linkedin_auth: Path to LinkedIn auth state file.
    """
    from bot.search import search_linkedin
    db: ApplicationDB = app.bot_data["db"]
    chat_id: int = app.bot_data["authorized_user_id"]
    bot = app.bot

    searches = await db.get_active_searches()
    if not searches:
        return

    now = datetime.now(timezone.utc).isoformat()
    total_new = 0

    for search in searches:
        try:
            results = await search_linkedin(search, auth_state_path=linkedin_auth)
        except Exception as e:
            logger.error("search poll error for %r: %s", search.query, e)
            await db.touch_search(search.id, now)
            continue

        for result in results:
            if await db.is_job_seen(result.url):
                continue
            await db.mark_job_seen(result.url, search.id)
            inserted = await db.enqueue_job(result.url, result.title, result.company, search.id)
            if inserted:
                total_new += 1

        await db.touch_search(search.id, now)

    if total_new == 0:
        return

    logger.info("search poll: %d new jobs queued", total_new)

    # Build and send the batch message so the user can pick which to investigate
    pending_jobs = await db.get_pending_queue()
    if not pending_jobs:
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_build_batch_message(pending_jobs, total_new),
            parse_mode="Markdown",
        )
        app.bot_data[PENDING_BATCH] = pending_jobs
    except Exception as e:
        logger.error("notify_search_matches: send error: %s", e)


def _build_batch_message(jobs: list["QueuedJob"], new_count: int | None = None) -> str:
    """Format a numbered list of queued jobs for the user to select from."""
    header = (
        f"\U0001f50d *{new_count} new job{'s' if new_count != 1 else ''} discovered!*\n\n"
        if new_count
        else f"\U0001f4cb *Job queue — {len(jobs)} pending*\n\n"
    )
    lines = [header]
    for i, job in enumerate(jobs, 1):
        lines.append(f"*{i}.* {job.title} \u2014 {job.company}")
    lines.append(
        "\nReply with numbers to investigate (e.g. *1,3*) or *all*.\n"
        "Type *skip all* to dismiss."
    )
    return "\n".join(lines)


async def _handle_batch_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Handle user's number selection in response to a batch job listing."""
    db: ApplicationDB = context.bot_data["db"]
    batch: list[QueuedJob] = context.bot_data.get(PENDING_BATCH, [])
    if not batch:
        context.bot_data.pop(PENDING_BATCH, None)
        return

    normalized = text.strip().lower()

    if normalized in ("skip all", "skip"):
        dismissed = await db.dismiss_all_queued()
        context.bot_data.pop(PENDING_BATCH, None)
        await update.message.reply_text(f"Dismissed {dismissed} job(s). Queue cleared.")
        return

    if normalized == "all":
        selected = list(batch)
    else:
        # Parse "1,3,5" or "1 3 5" or "1, 3, 5"
        indices: list[int] = []
        for part in re.split(r"[,\s]+", normalized):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1  # convert 1-based to 0-based
                if 0 <= idx < len(batch):
                    indices.append(idx)
        if not indices:
            await update.message.reply_text(
                "Reply with job numbers (e.g. 1,3) or 'all' to investigate, "
                "or 'skip all' to dismiss."
            )
            return
        selected = [batch[i] for i in indices]

    # Dismiss all non-selected pending jobs
    selected_ids = {j.id for j in selected}
    for job in batch:
        if job.id not in selected_ids:
            await db.update_queued_job_status(job.id, "dismissed")

    context.bot_data.pop(PENDING_BATCH, None)

    # Queue selected jobs for sequential processing
    context.bot_data[BATCH_QUEUE] = list(selected)
    count = len(selected)
    await update.message.reply_text(
        f"Investigating {count} job{'s' if count != 1 else ''}. "
        "I'll analyze them one at a time."
    )
    await _maybe_process_next_batch_item(update, context)


async def _maybe_process_next_batch_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Pop the next job from BATCH_QUEUE and start the analysis flow for it."""
    queue: list[QueuedJob] = context.bot_data.get(BATCH_QUEUE, [])
    if not queue:
        context.bot_data.pop(BATCH_QUEUE, None)
        return
    if context.user_data.get(PENDING_JOB):
        # Another job is already being handled — don't start the next one yet
        return
    next_job = queue.pop(0)
    if not queue:
        context.bot_data.pop(BATCH_QUEUE, None)
    else:
        context.bot_data[BATCH_QUEUE] = queue

    remaining = len(context.bot_data.get(BATCH_QUEUE, []))
    if remaining:
        await update.message.reply_text(
            f"({remaining} more in queue after this)"
        )
    await _handle_job_url(update, context, next_job.url)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending discovered jobs and let user select which to investigate.

    /queue
    """
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]
    pending = await db.get_pending_queue()

    if not pending:
        await update.message.reply_text(
            "Job queue is empty.\n\n"
            "Add saved searches with /search add <query> [location] — "
            "I'll poll them every 30 minutes and queue new matches here."
        )
        return

    context.bot_data[PENDING_BATCH] = pending
    await update.message.reply_text(
        _build_batch_message(pending),
        parse_mode="Markdown",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show application pipeline stats.

    /report
    """
    if not _auth(update, context):
        return
    db: ApplicationDB = context.bot_data["db"]

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    today_iso = (now - timedelta(hours=24)).isoformat()
    week_iso = (now - timedelta(days=7)).isoformat()

    today_stats = await db.get_stats(since_iso=today_iso)
    week_stats = await db.get_stats(since_iso=week_iso)
    all_stats = await db.get_stats()
    top_companies = await db.get_top_companies(limit=5)
    queue_count = await db.get_queue_count()

    def _fmt(stats: dict[str, int]) -> str:
        if not stats:
            return "  none"
        return "\n".join(
            f"  {status}: {count}"
            for status, count in sorted(stats.items(), key=lambda x: -x[1])
        )

    lines = ["Application Report\n"]

    lines.append("Last 24 hours:")
    lines.append(_fmt(today_stats))

    lines.append("\nLast 7 days:")
    lines.append(_fmt(week_stats))

    lines.append("\nAll time:")
    lines.append(_fmt(all_stats))

    lines.append(f"\nJob queue: {queue_count} pending review")
    if queue_count:
        lines.append("  /queue to review")

    if top_companies:
        lines.append("\nTop companies applied to:")
        for company, count in top_companies:
            lines.append(f"  {company} ({count})")

    await update.message.reply_text("\n".join(lines))


async def _submit_application(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingJob,
) -> None:
    registry: AdapterRegistry = context.bot_data["registry"]
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]

    adapter = registry.get(pending.url)
    resume_path = profile.get("resume_path", "")

    # Guard: don't double-submit the same job
    if await db.is_already_applied(pending.url):
        await update.message.reply_text(
            "Already have a successful application on record for this job — skipping to avoid duplicate."
        )
        context.user_data.pop(PENDING_JOB, None)
        return

    # Enforce rate limit before submitting
    prefs = load_preferences(profile)

    async def _notify_wait(msg: str) -> None:
        await update.message.reply_text(msg)

    try:
        await enforce_rate_limit(
            db,
            min_gap_minutes=prefs.min_apply_gap_minutes,
            max_gap_minutes=prefs.max_apply_gap_minutes,
            daily_cap=prefs.max_applies_per_day if prefs.max_applies_per_day > 0 else 30,
            notify=_notify_wait,
        )
    except RateLimitExceeded as e:
        await update.message.reply_text(str(e))
        context.user_data.pop(PENDING_JOB, None)
        return

    await update.message.reply_text("Submitting application...")

    try:
        result = await adapter.submit_application(pending.url, pending.fields, resume_path)
    except Exception as e:
        await update.message.reply_text(f"Application failed: {e}\nNothing was submitted.")
        context.user_data.pop(PENDING_JOB, None)
        return

    # Special short-circuit: job closed or already applied (detected by adapter)
    if result.closed:
        await update.message.reply_text(
            f"Job is no longer accepting applications — nothing submitted.\n{result.error}"
        )
        context.user_data.pop(PENDING_JOB, None)
        return

    if result.already_applied:
        await update.message.reply_text(
            "Already applied to this job (site confirmed) — nothing submitted."
        )
        context.user_data.pop(PENDING_JOB, None)
        return

    # Record in DB
    submitted_json = json.dumps(result.submitted_fields)
    record = ApplicationRecord(
        url=pending.url,
        title=pending.job_info.title,
        company=pending.job_info.company,
        site=adapter.name,
        status="applied" if result.success else "failed",
        submitted_fields=submitted_json,
        screenshot_path=result.screenshot_path,
        applied_at=datetime.now(timezone.utc).isoformat() if result.success else None,
        notes=result.error or "",
        cover_letter=pending.cover_letter,
        tailored_resume=pending.tailored_resume,
    )
    app_id = await db.insert_application(record)

    context.user_data.pop(PENDING_JOB, None)
    context.user_data.pop(AWAITING_FIELD, None)

    if result.success:
        # Send screenshot
        if result.screenshot_path:
            try:
                with open(result.screenshot_path, "rb") as f:
                    await update.message.reply_photo(f)
            except Exception as photo_err:
                logger.warning("Could not send screenshot %s: %s", result.screenshot_path, photo_err)

        # Confirmation status
        if result.submission_confirmed:
            confirmation_line = "Submission confirmed by site."
        else:
            confirmation_line = "Submitted (unconfirmed — review screenshot to verify)."

        # Field summary (cap at 15 fields)
        field_lines = [f"  {k}: {v[:80]}" for k, v in list(result.submitted_fields.items())[:15]]
        extras = []
        if record.tailored_resume:
            extras.append(f"/resume {app_id}")
        if record.cover_letter:
            extras.append(f"/coverletter {app_id}")
        extra_str = "\n\nRetrieve: " + " | ".join(extras) if extras else ""

        msg = (
            f"Application submitted! (ID: {app_id})\n"
            f"{confirmation_line}\n\n"
            "Submitted:\n" + "\n".join(field_lines) + extra_str
        )

        # Warn about any fields we couldn't verify were filled
        if result.missing_fields:
            msg += (
                f"\n\nWarning: could not verify these fields were filled: "
                f"{', '.join(result.missing_fields)}"
            )

        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(
            f"Application failed: {result.error}\nNothing was submitted. (ID: {app_id})"
        )

    await _maybe_process_next_batch_item(update, context)
