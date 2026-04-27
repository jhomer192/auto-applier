import asyncio
import functools
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
from bot.fit import evaluate_fit, fit_summary_lines, score_breakdown
from bot.inbox import classify_email, GmailInbox
from bot.llm import analyze_job, claude_call, draft_outreach_message, extract_achievements, extract_voice_profile, generate_cover_letter, generate_field_answer, generate_interview_prep, LLMError, tailor_resume
from bot.voice import load_voice_profile, save_voice_profile, voice_profile_summary
from bot.models import ApplicationRecord, EmailThread, FitReport, JobPreferences, PendingJob, QueuedJob, SavedSearch
from bot.referral_radar import find_referral_candidates
from bot.profile import load_preferences, save_preferences
from bot.scam_detector import check_scam
from bot.scraper import field_answer_hint

logger = logging.getLogger(__name__)

# Conversation state keys stored in context.user_data
PENDING_JOB = "pending_job"
AWAITING_FIELD = "awaiting_field"
AWAITING_EMAIL_REPLY = "awaiting_email_reply"  # value: EmailThread

# Profile interview state
PROFILE_INTERVIEW = "profile_interview"   # value: {"step": int, "answers": [(q, a), ...]}

# Voice fingerprinting interview state
VOICE_INTERVIEW = "VOICE_INTERVIEW"   # value: {"step": int, "samples": [str, ...]}

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

VOICE_PROMPTS = [
    "Tell me about a project you're proud of. Write it in your own words, like you'd explain it to a friend. (2-4 sentences)",
    "Describe why you want to work in your target industry. Be genuine — this is just for calibration, it won't be sent anywhere. (2-4 sentences)",
    "What's something you've learned recently that excited you? (2-4 sentences)",
]

SUPPORTED_PATTERNS = [
    r"linkedin\.com/jobs/view/\d+",
    r"boards\.greenhouse\.io/.+",
    r"jobs\.lever\.co/.+",
]


def _is_job_url(text: str) -> bool:
    return any(re.search(p, text) for p in SUPPORTED_PATTERNS)


# Telegram has a hard limit of 4096 chars per message. Resumes, cover letters,
# audit reports, and LLM-composed emails routinely exceed this and cause
# `BadRequest: Message is too long`, killing the handler silently.
TELEGRAM_MAX_CHARS = 4096
_CHUNK_SAFETY_MARGIN = 96  # leave room for "(part N/M)\n\n" prefix


def _split_for_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS - _CHUNK_SAFETY_MARGIN) -> list[str]:
    """Split `text` into chunks no larger than `limit`, preferring paragraph then line breaks.

    Falls back to a hard character cut only if no break is found in the chunk window.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer paragraph break, then line break, then word break, then hard cut.
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > limit // 2:  # only use the break if it's reasonably late in the window
                chunks.append(remaining[:cut])
                remaining = remaining[cut + len(sep):]
                break
        else:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def reply_chunked(message, text: str, parse_mode: str | None = None) -> None:
    """Send `text` as one or more Telegram replies, splitting safely under the 4096 limit.

    For multi-chunk sends, prepends "(part N/M)\\n\\n" to each chunk so the user
    knows the message is continued. Markdown parse_mode is preserved per-chunk.
    """
    chunks = _split_for_telegram(text)
    if len(chunks) == 1:
        await message.reply_text(chunks[0], parse_mode=parse_mode)
        return
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"(part {i}/{total})\n\n"
        await message.reply_text(prefix + chunk, parse_mode=parse_mode)


async def send_chunked(bot, chat_id: int, text: str, parse_mode: str | None = None) -> None:
    """Same as `reply_chunked` but for background tasks that have a Bot + chat_id, not a message."""
    chunks = _split_for_telegram(text)
    if len(chunks) == 1:
        await bot.send_message(chat_id=chat_id, text=chunks[0], parse_mode=parse_mode)
        return
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"(part {i}/{total})\n\n"
        await bot.send_message(chat_id=chat_id, text=prefix + chunk, parse_mode=parse_mode)


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
        linkedin_auth: str = "data/linkedin_auth.json",
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._profile = profile
        self._registry = registry
        self._screenshot_dir = screenshot_dir
        self._gmail_inbox = gmail_inbox
        self._profile_path = profile_path
        self._linkedin_auth = linkedin_auth

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
        app.bot_data["linkedin_auth"] = self._linkedin_auth

        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("history", cmd_history))
        app.add_handler(CommandHandler("cancel", cmd_cancel))
        app.add_handler(CommandHandler("search", cmd_search))
        app.add_handler(CommandHandler("resume", cmd_resume))
        app.add_handler(CommandHandler("coverletter", cmd_coverletter))
        app.add_handler(CommandHandler("profile", cmd_profile))
        app.add_handler(CommandHandler("voice", cmd_voice))
        app.add_handler(CommandHandler("prefs", cmd_prefs))
        app.add_handler(CommandHandler("queue", cmd_queue))
        app.add_handler(CommandHandler("report", cmd_report))
        app.add_handler(CommandHandler("linkedin", cmd_linkedin))
        app.add_handler(CommandHandler("website", cmd_website))
        app.add_handler(CommandHandler("sources", cmd_sources))
        app.add_handler(CommandHandler("handshake", cmd_handshake))
        app.add_handler(CommandHandler("referrals", cmd_referrals))
        app.add_handler(CommandHandler("scams", cmd_scams))
        app.add_handler(CommandHandler("scam_apply", cmd_scam_apply))
        app.add_handler(CommandHandler("force", cmd_force))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        return app

    def run(self) -> None:
        app = self.build_app()
        logger.info("Bot starting...")
        app.run_polling(drop_pending_updates=True)


def _auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True only if message is from the authorized chat."""
    return bool(update.effective_user and update.effective_user.id == context.bot_data["authorized_user_id"])


def requires_auth(handler):
    """Decorator: run the handler only if the update is from the authorized user.

    Audit fix #7. Previously every command handler called ``_auth(update, context)``
    inline as its first statement, which meant any new handler that forgot the
    check would silently leak data. Centralising the gate in a decorator makes it
    impossible to register an unguarded handler — and the decorator runs before
    any of the body, so even argument parsing happens after the auth check.

    Unauthorised updates are dropped silently (no reply, no DB writes, no
    bot.send_message calls).
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not _auth(update, context):
            return None
        return await handler(update, context, *args, **kwargs)
    return wrapper


@requires_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]
    prefs = load_preferences(profile)

    recent_count = len(await db.get_recent(limit=200))
    has_roles = bool(prefs.desired_roles)
    has_searches = bool(await db.get_active_searches())
    queue_count = await db.get_queue_count()

    if recent_count == 0 and not has_roles:
        # First-time user — walk them through the two most important things
        await update.message.reply_text(
            "Hey! I apply for jobs automatically — you set your preferences once, "
            "then I find matching jobs, write tailored resumes and cover letters, "
            "and submit applications for you.\n\n"
            "Two things to do first:\n\n"
            "1. Tell me what roles you want:\n"
            "   /prefs roles Software Engineer, Backend Engineer\n\n"
            "2. Tell me what you're looking for:\n"
            "   /prefs salary 120000\n"
            "   /prefs arrangement remote\n"
            "   /prefs seniority junior,mid,senior\n\n"
            "After that, just send me any job URL and I'll handle the application. "
            "Or turn on auto-search and I'll find jobs for you:\n"
            "   /prefs autosearch on\n\n"
            "Type anything if you have questions — I'll help."
        )
    elif queue_count > 0:
        await update.message.reply_text(
            f"Welcome back. You have {queue_count} job{'s' if queue_count != 1 else ''} "
            f"waiting in the queue.\n\n"
            "Use /queue to review them, or send me a new job URL to apply directly."
        )
    else:
        await update.message.reply_text(
            f"Welcome back. {recent_count} application{'s' if recent_count != 1 else ''} sent so far.\n\n"
            "Send me a job URL to apply, or use /queue to check for discovered jobs. "
            "Type anything if you have questions."
        )


@requires_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Auto Job Applier\n\n"
        "Send any LinkedIn Easy Apply, Greenhouse, or Lever URL to start an application.\n"
        "Reply Y to apply, N to skip. Or let auto-apply handle it (see /prefs).\n\n"

        "── Preferences ──\n"
        "/prefs \u2014 show all current preferences\n"
        "/prefs roles <role1,role2> \u2014 set target roles\n"
        "/prefs salary <min> [target] \u2014 salary floor + target (annual, USD)\n"
        "/prefs seniority <levels> \u2014 junior | mid | senior | staff | principal | director\n"
        "/prefs arrangement <modes> \u2014 remote | hybrid | onsite (comma-separate for multiple)\n"
        "/prefs autoapply <0\u2013100> \u2014 auto-submit threshold; 0 = always ask\n"
        "/prefs autosearch on|off \u2014 auto-generate searches from your roles\n"
        "/prefs sponsorship yes|no \u2014 filter for jobs that sponsor visas\n"
        "/prefs exclude <company> \u2014 hard-skip a company\n"
        "/prefs unexclude <company> \u2014 remove from skip list\n\n"

        "── Job discovery ──\n"
        "/search add <query> [in <location>] \u2014 save a LinkedIn search\n"
        "/search list \u2014 list saved searches\n"
        "/search rm <id> \u2014 remove a search\n"
        "/queue \u2014 pending discovered jobs \u2014 reply with numbers (1,3) or all or skip all\n"
        "/scams \u2014 view blocked/flagged job postings\n"
        "/scam_apply <n> \u2014 approve nth flagged job for processing\n"
        "/force <url> \u2014 process a URL that was blocked by scam detector\n"
        "/sources \u2014 show active discovery sources (GitHub repos, company boards)\n"
        "/handshake \u2014 Handshake connection status and setup\n"
        "/report \u2014 stats (today / week / all-time) + queue size\n\n"

        "── Application history ──\n"
        "/status \u2014 application counts by status\n"
        "/history [N] \u2014 last N applications (default 10)\n"
        "/resume <id> \u2014 retrieve tailored resume\n"
        "/coverletter <id> \u2014 retrieve cover letter\n"
        "/referrals <id> \u2014 view referral candidates for an application\n\n"

        "── Profile & branding ──\n"
        "/profile \u2014 add achievements to your profile\n"
        "/voice \u2014 calibrate writing style so cover letters sound like you\n"
        "/linkedin [url] \u2014 audit your LinkedIn (scored section-by-section feedback)\n"
        "/website [minimal|dark|academic] \u2014 generate a GitHub Pages portfolio\n"
        "/website guide \u2014 deploy instructions\n\n"

        "/cancel \u2014 dismiss the current pending item\n"
        "/help \u2014 this message"
    )


@requires_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


@requires_auth
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


@requires_auth
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

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

    elif context.user_data.get(VOICE_INTERVIEW):
        context.user_data.pop(VOICE_INTERVIEW, None)
        await update.message.reply_text("Voice calibration cancelled. Nothing was saved.")

    else:
        await update.message.reply_text("Nothing to cancel.")


async def _handle_conversational(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Fallback for any message that isn't a command, URL, or known response.

    Uses Claude to interpret the user's intent and respond helpfully in plain
    language.  Works for greetings, confused users, natural-language preference
    expressions ("I want remote SWE jobs in NYC"), and anything else.
    """
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]
    prefs = load_preferences(profile)

    recent_count = len(await db.get_recent(limit=200))
    queue_count = await db.get_queue_count()
    searches = await db.get_active_searches()

    roles_str = ", ".join(prefs.desired_roles) if prefs.desired_roles else "not set"
    salary_str = f"${prefs.min_salary:,}" if prefs.min_salary else "not set"
    seniority_str = ", ".join(prefs.seniority) if prefs.seniority else "any"
    arrangement_str = ", ".join(prefs.work_arrangement) if prefs.work_arrangement else "any"
    searches_str = (
        ", ".join(f'"{s.query}"' for s in searches) if searches else "none set up yet"
    )

    prompt = (
        "You are a helpful Telegram bot that automates job applications for people.\n\n"
        "The user sent a message you need to interpret and respond to helpfully.\n"
        "Respond in plain conversational text — no markdown, no bullet points, "
        "2–4 sentences max. Be warm, clear, and give them exactly one actionable next step.\n\n"
        "What this bot can do:\n"
        "- Paste any LinkedIn Easy Apply, Greenhouse, or Lever job URL → bot analyzes it, "
        "writes a tailored resume and cover letter, asks Y/N to apply\n"
        "- /search add <role> [in <location>] → bot checks LinkedIn every 30 min, "
        "queues matching new jobs automatically\n"
        "- /queue → see discovered jobs and pick which ones to look at\n"
        "- /prefs roles <role1,role2> → tell the bot what jobs you want\n"
        "- /prefs salary <min> → set your salary floor\n"
        "- /prefs seniority junior|mid|senior|staff → filter by level\n"
        "- /prefs arrangement remote|hybrid|onsite → filter by location type\n"
        "- /prefs autoapply 80 → bot auto-submits jobs scoring 80+ without asking\n"
        "- /profile → run an interview to add achievements to your profile\n"
        "- /sources → see GitHub new-grad repos and company job boards being monitored\n\n"
        f"Their current setup:\n"
        f"  Desired roles: {roles_str}\n"
        f"  Min salary: {salary_str}\n"
        f"  Seniority: {seniority_str}\n"
        f"  Arrangement: {arrangement_str}\n"
        f"  Active searches: {searches_str}\n"
        f"  Applications sent: {recent_count}\n"
        f"  Jobs waiting in queue: {queue_count}\n\n"
        f"User's message: {text}\n\n"
        "If they seem to be describing job preferences in natural language "
        "(e.g. 'I want remote software jobs in NYC'), extract what they said and "
        "give them the exact /prefs commands to run. "
        "If they seem lost or are greeting you, briefly explain the single most "
        "useful thing they can do right now given their setup above."
    )

    try:
        response = await claude_call(prompt)
        await update.message.reply_text(response)
    except LLMError:
        await update.message.reply_text(
            "Send me a job URL to apply, or use /help to see what I can do."
        )


@requires_auth
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    text = update.message.text.strip()
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]
    registry: AdapterRegistry = context.bot_data["registry"]

    # Case 1a: waiting for a recruiter email reply from the user
    # (stored as a queue in bot_data because background tasks can't access user_data)
    if context.bot_data.get(AWAITING_EMAIL_REPLY):
        await _handle_email_reply(update, context, text)
        return

    # Case 1b: voice fingerprinting interview in progress
    if context.user_data.get(VOICE_INTERVIEW):
        await _handle_voice_answer(update, context, text)
        return

    # Case 1c: profile interview in progress
    if context.user_data.get(PROFILE_INTERVIEW):
        await _handle_profile_answer(update, context, text)
        return

    # Case 1d: we are waiting for a field answer from the user
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

    # Case 4: unrecognized — use Claude to understand intent and guide them
    await _handle_conversational(update, context, text)


@requires_auth
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage saved job searches.

    /search add <query> [location] — save a new periodic search
    /search list                   — show all saved searches
    /search rm <id>                — deactivate a search
    """
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


@requires_auth
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the tailored resume for a past application.

    /resume <id>
    """
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
    await reply_chunked(update.message, header + record.tailored_resume)


@requires_auth
async def cmd_coverletter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the cover letter for a past application.

    /coverletter <id>
    """
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
    await reply_chunked(update.message, header + record.cover_letter)


@requires_auth
async def cmd_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral candidates for a given application.

    /referrals <app_id>
    """
    db: ApplicationDB = context.bot_data["db"]
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /referrals <application id>")
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

    candidates = await db.get_referral_candidates(app_id)
    if not candidates:
        await update.message.reply_text(
            f"No referral candidates found for application {app_id} "
            f"({record.title} @ {record.company}).\n"
            "Referral radar runs automatically after each application."
        )
        return

    lines = [f"Referral candidates for [{app_id}] {record.title} @ {record.company}:"]
    for i, candidate in enumerate(candidates, 1):
        conn_label = f" ({candidate.connection_type})" if candidate.connection_type else ""
        lines.append(f"\n{i}. {candidate.name}{conn_label}")
        if candidate.headline:
            lines.append(f"   {candidate.headline}")
        if candidate.linkedin_url:
            lines.append(f"   {candidate.linkedin_url}")
        if candidate.draft_message:
            lines.append(f"   Draft: {candidate.draft_message}")

    await reply_chunked(update.message, "\n".join(lines))


@requires_auth
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
    /prefs autosearch on|off     — auto-generate searches from desired_roles
    """

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
        sponsorship = "yes (need sponsorship)" if prefs.requires_sponsorship else "no"
        auto_search = "on" if prefs.auto_search else "off"
        await update.message.reply_text(
            "Current job preferences:\n\n"
            f"Roles: {roles}\n"
            f"  \u2192 /prefs roles Backend Engineer,Staff Engineer\n\n"
            f"Min salary: {min_s}  |  Target: {target_s}\n"
            f"  \u2192 /prefs salary 180000 220000\n\n"
            f"Seniority: {seniority}\n"
            f"  options: junior, mid, senior, staff, principal, director\n"
            f"  \u2192 /prefs seniority senior,staff\n\n"
            f"Arrangement: {arrangement}\n"
            f"  options: remote, hybrid, onsite\n"
            f"  \u2192 /prefs arrangement remote,hybrid\n\n"
            f"Auto-apply: {auto}\n"
            f"  0\u2013100 score threshold; 0 = always ask Y/N\n"
            f"  \u2192 /prefs autoapply 85\n\n"
            f"Auto-search: {auto_search}\n"
            f"  auto-generates LinkedIn searches from your roles\n"
            f"  \u2192 /prefs autosearch on|off\n\n"
            f"Visa sponsorship needed: {sponsorship}\n"
            f"  \u2192 /prefs sponsorship yes|no\n\n"
            f"Excluded companies: {excluded}\n"
            f"  \u2192 /prefs exclude Google  |  /prefs unexclude Google"
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

    elif sub == "autosearch":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            await update.message.reply_text("Usage: /prefs autosearch on|off")
            return
        prefs.auto_search = args[1].lower() == "on"
        save_preferences(profile, prefs, profile_path)
        context.bot_data["profile"] = profile
        if prefs.auto_search:
            roles = ", ".join(prefs.desired_roles) if prefs.desired_roles else "none set yet"
            await update.message.reply_text(
                f"Auto-search enabled.\n"
                f"I'll create searches for your desired roles ({roles}) automatically.\n"
                "Set roles with /prefs roles <role1,role2,...>"
            )
        else:
            await update.message.reply_text(
                "Auto-search disabled. Use /search add <query> to add searches manually."
            )

    else:
        await update.message.reply_text(
            "Unknown subcommand. Options: roles, salary, seniority, arrangement, "
            "autoapply, autosearch, exclude, unexclude, sponsorship"
        )


@requires_auth
async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start voice fingerprinting or show/reset the current voice profile.

    /voice        — start the interview (or show status if profile exists)
    /voice reset  — clear existing profile and restart
    """

    args = context.args or []
    is_reset = args and args[0].lower() == "reset"

    existing = load_voice_profile()

    if is_reset:
        import os as _os
        from bot.voice import get_voice_profile_path
        vp_path = get_voice_profile_path()
        try:
            _os.remove(vp_path)
        except FileNotFoundError:
            pass
        context.user_data.pop(VOICE_INTERVIEW, None)
        await update.message.reply_text(
            "Voice profile cleared. Starting fresh calibration.\n\n"
            f"*Question 1 of {len(VOICE_PROMPTS)}:*\n{VOICE_PROMPTS[0]}",
            parse_mode="Markdown",
        )
        context.user_data[VOICE_INTERVIEW] = {"step": 0, "samples": []}
        return

    if existing and not is_reset:
        summary = voice_profile_summary(existing)
        await update.message.reply_text(
            "Voice profile already set:\n\n"
            f"{summary}\n\n"
            "Use /voice reset to redo the calibration."
        )
        return

    if context.user_data.get(VOICE_INTERVIEW):
        state = context.user_data[VOICE_INTERVIEW]
        step = state["step"]
        await update.message.reply_text(
            f"Voice calibration already in progress. Answer question {step + 1} above, "
            "or /cancel to quit."
        )
        return

    context.user_data[VOICE_INTERVIEW] = {"step": 0, "samples": []}
    await update.message.reply_text(
        "Let's calibrate your writing style. I'll ask 3 short questions — write naturally, "
        "like you're texting a friend. The answers stay local and are only used to "
        "make your cover letters sound like you.\n\n"
        f"*Question 1 of {len(VOICE_PROMPTS)}:*\n{VOICE_PROMPTS[0]}",
        parse_mode="Markdown",
    )


async def _handle_voice_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Process one answer in the voice fingerprinting interview.

    Args:
        update: Telegram Update.
        context: Handler context.
        text: The user's answer text.
    """
    state: dict = context.user_data[VOICE_INTERVIEW]
    step: int = state["step"]
    samples: list = state["samples"]

    samples.append(text)
    step += 1
    state["step"] = step
    state["samples"] = samples

    if step < len(VOICE_PROMPTS):
        await update.message.reply_text(
            f"*Question {step + 1} of {len(VOICE_PROMPTS)}:*\n{VOICE_PROMPTS[step]}",
            parse_mode="Markdown",
        )
        return

    # All samples collected — extract and save
    context.user_data.pop(VOICE_INTERVIEW, None)
    await update.message.reply_text("Analyzing your writing style...")

    try:
        vp = await extract_voice_profile(samples)
    except LLMError as e:
        await update.message.reply_text(f"Could not extract voice profile: {e}")
        return

    from datetime import datetime as _dt, timezone as _tz
    vp["samples_collected"] = len(samples)
    vp["created_at"] = _dt.now(_tz.utc).isoformat(timespec="seconds")

    try:
        save_voice_profile(vp)
    except Exception as e:
        await update.message.reply_text(f"Could not save voice profile: {e}")
        return

    summary = voice_profile_summary(vp)
    await update.message.reply_text(
        "Voice profile saved. Future cover letters and field answers will match your style.\n\n"
        f"{summary}\n\n"
        "Use /voice reset to redo calibration."
    )


@requires_auth
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start an achievement-mining interview to enrich the candidate profile."""
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


async def _handle_job_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    skip_scam_check: bool = False,
) -> None:
    """Handle a job URL: fetch, analyze, evaluate fit, generate materials, then show informed Y/N.

    Args:
        update: Telegram Update object.
        context: Handler context.
        url: Job posting URL.
        skip_scam_check: When True, bypass the scam gate (used by /force command).
    """
    registry: AdapterRegistry = context.bot_data["registry"]
    profile: dict = context.bot_data["profile"]
    db: ApplicationDB = context.bot_data["db"]

    if not skip_scam_check:
        scam = check_scam(url, title="", company="")
        if scam.verdict == "rejected":
            await update.message.reply_text(
                f"Strong scam signals ({scam.score}/100):\n"
                + "\n".join(f"• {s}" for s in scam.signals)
                + f"\n\nBlocked. Send `/force {url}` to process it anyway."
            )
            return
        if scam.verdict == "flagged":
            signal_text = "\n".join(f"• {s}" for s in scam.signals)
            await update.message.reply_text(
                f"Warning: scam signals detected ({scam.score}/100):\n{signal_text}\n\n"
                "Proceeding with analysis — use caution."
            )

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

    # Load voice profile once for all LLM calls in this flow
    voice_profile = load_voice_profile()

    # Fetch company news for cover letter hook
    from bot.news import fetch_company_news
    news_items = []
    try:
        news_items = await asyncio.wait_for(fetch_company_news(job_info.company), timeout=8.0)
    except Exception:
        pass

    # Generate tailored materials
    tailored_resume_text = ""
    cover_letter_text = ""
    try:
        tailored_resume_text = await tailor_resume(job_analysis, profile)
    except LLMError as e:
        logger.warning("tailor_resume failed: %s", e)
    try:
        cover_letter_text = await generate_cover_letter(
            job_analysis, profile, voice_profile=voice_profile, news_items=news_items
        )
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
                voice_profile=voice_profile,
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

    breakdown = score_breakdown(job_analysis, prefs)
    breakdown_block = f"\n\n{breakdown}"

    cover_preview = ""
    if cover_letter_text:
        cover_preview = "\n\nCover letter preview:\n" + cover_letter_text[:300] + (
            "..." if len(cover_letter_text) > 300 else ""
        )
    manual_note = ""
    if needs_user_input:
        manual_note = f"\n\n({len(needs_user_input)} field(s) need your input after Y)"

    threshold_hint = ""
    if prefs.auto_apply_threshold > 0 and job_analysis.match_score < prefs.auto_apply_threshold:
        gap = prefs.auto_apply_threshold - job_analysis.match_score
        threshold_hint = f"\n(Score is {gap} pts below your auto-apply threshold of {prefs.auto_apply_threshold})"

    context.user_data[PENDING_JOB] = pending

    # Run referral radar before showing the Y/N prompt (non-blocking lookup)
    referral_block = ""
    radar_enabled = profile.get("referral_radar", {}).get("enabled", True)
    if radar_enabled:
        try:
            linkedin_auth: str = context.bot_data.get("linkedin_auth", "data/linkedin_auth.json")
            user_school = ""
            edu = profile.get("education") or []
            if edu and isinstance(edu, list) and isinstance(edu[0], dict):
                user_school = edu[0].get("institution", "")
            user_companies = [
                j["company"] for j in (profile.get("work_history") or [])
                if isinstance(j, dict) and j.get("company")
            ]
            radar_candidates = await find_referral_candidates(
                company=job_info.company,
                user_school=user_school,
                user_companies=user_companies,
                linkedin_auth=linkedin_auth,
                max_results=3,
                timeout=20,
            )
            if radar_candidates:
                ref_lines = ["\nReferral leads:"]
                for i, rc in enumerate(radar_candidates, 1):
                    conn_label = f" ({rc.connection_type})" if rc.connection_type else ""
                    ref_lines.append(f"{i}. {rc.name}{conn_label} — {rc.headline[:60]}")
                referral_block = "\n".join(ref_lines)
        except Exception as radar_err:
            logger.warning("referral_radar: inline lookup failed: %s", radar_err)

    await update.message.reply_text(
        f"*{job_info.title}* at *{job_info.company}*"
        f"{fit_block}"
        f"{breakdown_block}"
        f"{threshold_hint}"
        f"{referral_block}"
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
        await reply_chunked(update.message, f"Sent to {thread.from_address}:\n\n{body}")
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
    return await claude_call(prompt)


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
    return await claude_call(prompt)


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

            # Interview invite → send a tailored prep guide immediately
            if category == "interview":
                try:
                    # Pull company/title from the linked application if available
                    app_record = None
                    if email_thread.app_id:
                        app_record = await db.get_by_id(email_thread.app_id)

                    company = app_record.company if app_record else email_thread.from_address
                    title = app_record.title if app_record else email_thread.subject
                    context_text = (
                        f"Email subject: {email_thread.subject}\n"
                        f"Email preview: {email_thread.body_preview}\n"
                    )
                    if app_record and app_record.cover_letter:
                        context_text += f"\nCover letter sent:\n{app_record.cover_letter[:800]}"

                    prep = await generate_interview_prep(company, title, context_text)
                    await send_chunked(
                        bot,
                        chat_id,
                        f"📚 Interview prep — {title} at {company}:\n\n{prep}",
                    )
                except Exception as prep_err:
                    logger.warning("notify_new_emails: interview prep failed: %s", prep_err)
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
    dismissed_count = 0
    for job in batch:
        if job.id not in selected_ids:
            await db.update_queued_job_status(job.id, "dismissed")
            dismissed_count += 1

    context.bot_data.pop(PENDING_BATCH, None)

    # Queue selected jobs for sequential processing
    context.bot_data[BATCH_QUEUE] = list(selected)
    count = len(selected)
    dismissed_note = f" ({dismissed_count} others dismissed)" if dismissed_count else ""
    await update.message.reply_text(
        f"Investigating {count} job{'s' if count != 1 else ''}{dismissed_note}. "
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


@requires_auth
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending discovered jobs and let user select which to investigate.

    /queue
    """
    db: ApplicationDB = context.bot_data["db"]
    pending = await db.get_pending_queue()

    retryable_failed, permanent_failed = await db.get_failed_counts()

    if not pending:
        suffix = ""
        if retryable_failed or permanent_failed:
            suffix_parts = []
            if retryable_failed:
                suffix_parts.append(f"{retryable_failed} failed (will retry)")
            if permanent_failed:
                suffix_parts.append(f"{permanent_failed} failed permanently")
            suffix = "\n\n" + ", ".join(suffix_parts) + "."
        await update.message.reply_text(
            "Job queue is empty.\n\n"
            "Add saved searches with /search add <query> [location] — "
            "I'll poll them every 30 minutes and queue new matches here." + suffix
        )
        return

    context.bot_data[PENDING_BATCH] = pending
    msg = _build_batch_message(pending)
    if retryable_failed or permanent_failed:
        extra_lines = []
        if retryable_failed:
            extra_lines.append(f"{retryable_failed} job(s) failed transiently — auto-retrying.")
        if permanent_failed:
            extra_lines.append(f"{permanent_failed} job(s) failed after 3 attempts (won't retry).")
        msg += "\n\n_" + " ".join(extra_lines) + "_"
    await update.message.reply_text(msg, parse_mode="Markdown")


@requires_auth
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show application pipeline stats.

    /report
    """
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

    retryable_failed, permanent_failed = await db.get_failed_counts()
    if retryable_failed or permanent_failed:
        lines.append("\nFailed jobs:")
        if retryable_failed:
            lines.append(f"  retryable: {retryable_failed}  (will be re-queued automatically)")
        if permanent_failed:
            lines.append(f"  permanent: {permanent_failed}  (3+ attempts, no further retry)")

    if top_companies:
        lines.append("\nTop companies applied to:")
        for company, count in top_companies:
            lines.append(f"  {company} ({count})")

    await update.message.reply_text("\n".join(lines))


@requires_auth
async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active job discovery sources and their configuration.

    /sources
    """

    import os
    from bot.sources import ALL_SOURCES

    github_token = bool(os.getenv("GITHUB_TOKEN"))
    source_poll_interval = int(os.getenv("SOURCE_POLL_INTERVAL", "3600"))
    minutes = source_poll_interval // 60

    lines = ["📡 Job Discovery Sources\n"]
    for source in ALL_SOURCES:
        if source.name == "github_newgrad":
            lines.append(
                "✅ GitHub New Grad — active\n"
                "   SimplifyJobs, speedyapply, vanshb03 repos (updated daily)\n"
                "   Covers SWE, quant, finance new-grad roles"
            )
        elif source.name == "company_pages":
            lines.append(
                "✅ Company Pages — active\n"
                "   35+ companies via Greenhouse & Lever JSON APIs\n"
                "   Includes: Stripe, Anthropic, Figma, Two Sigma, Citadel, HRT, Optiver, ..."
            )
        elif source.name == "github_orgs":
            status = "✅ GitHub Orgs — active" if github_token else "❌ GitHub Orgs — inactive"
            note = (
                "   Discovers companies via GitHub org metadata"
                if github_token
                else "   Set GITHUB_TOKEN in .env to enable"
            )
            lines.append(f"{status}\n{note}")
        elif source.name == "handshake":
            from bot.sources.handshake import HandshakeSource
            from pathlib import Path as _Path
            hs: HandshakeSource | None = context.bot_data.get("handshake_source")
            hs_auth = os.getenv("HANDSHAKE_AUTH_STATE", "data/handshake_auth.json")
            if hs and hs._session_expired:
                hs_status = "⚠️ Handshake — session expired"
                hs_note = "   Re-authenticate: DISPLAY=:0 python setup/handshake_login.py"
            elif _Path(hs_auth).exists():
                hs_status = "✅ Handshake — connected"
                hs_note = "   Campus/new-grad roles via Handshake GraphQL"
            else:
                hs_status = "❌ Handshake — not connected"
                hs_note = "   Run: DISPLAY=:0 python setup/handshake_login.py"
            lines.append(f"{hs_status}\n{hs_note}")
        elif source.name == "yc_batch":
            lines.append(
                "✅ YC Batch Intelligence — active\n"
                "   Queries YC public API for hiring companies from W26, S25, W25\n"
                "   Probes each company's Greenhouse & Lever board for matching roles"
            )

    lines.append(f"\nPolls every {minutes} min. Jobs matching your desired roles are queued automatically.")
    lines.append("Use /queue to review, or set /prefs autoapply <score> for hands-free mode.")

    await update.message.reply_text("\n".join(lines))


@requires_auth
async def cmd_handshake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Handshake connection status and setup instructions.

    /handshake
    """

    from bot.sources.handshake import HandshakeSource
    from pathlib import Path as _Path

    hs: HandshakeSource | None = context.bot_data.get("handshake_source")
    hs_auth = os.getenv("HANDSHAKE_AUTH_STATE", "data/handshake_auth.json")

    if hs and hs._session_expired:
        await update.message.reply_text(
            "Handshake — session expired\n\n"
            "Re-authenticate: DISPLAY=:0 python setup/handshake_login.py"
        )
    elif _Path(hs_auth).exists():
        await update.message.reply_text(
            "Handshake — connected\n\n"
            "Poll interval: every hour\n"
            "Use /queue to review discovered jobs.\n\n"
            "To re-authenticate: DISPLAY=:0 python setup/handshake_login.py"
        )
    else:
        await update.message.reply_text(
            "Handshake — not connected\n\n"
            "To connect:\n"
            "1. On the VPS, run: DISPLAY=:0 python setup/handshake_login.py\n"
            "2. Log in with your .edu or alumni account\n"
            "3. Handshake jobs will appear in /queue\n\n"
            "Requires a Handshake account (current student or recent grad)."
        )


@requires_auth
async def cmd_scams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show rejected and flagged scam postings.

    /scams
    """
    db: ApplicationDB = context.bot_data["db"]

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    rejected = await db.get_rejected_jobs(limit=50)
    flagged = await db.get_flagged_queue()

    recent_rejected = [r for r in rejected if r["rejected_at"] >= week_ago]

    lines = [f"Scam Filter Summary\n"]
    lines.append(f"Rejected (last 7 days): {len(recent_rejected)}")
    lines.append(f"Flagged (pending review): {len(flagged)}\n")

    if recent_rejected:
        lines.append("-- Recently Rejected --")
        for r in recent_rejected[:10]:
            signals_preview = r["signals"].replace("|", ", ") if r["signals"] else "no signals"
            lines.append(f"[{r['scam_score']}/100] {r['title']} @ {r['company']}")
            lines.append(f"  Signals: {signals_preview}")
            lines.append(f"  {r['url'][:80]}")

    if flagged:
        lines.append("\n-- Flagged Queue (use /scam_apply <n> to approve) --")
        for i, job in enumerate(flagged, 1):
            signals_preview = job.scam_signals.replace("|", ", ") if job.scam_signals else "no signals"
            lines.append(f"{i}. [{job.scam_score}/100] {job.title} @ {job.company}")
            lines.append(f"   Signals: {signals_preview}")

    if not recent_rejected and not flagged:
        lines.append("No scam-flagged or rejected jobs found.")

    await update.message.reply_text("\n".join(lines))


@requires_auth
async def cmd_scam_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a flagged job for normal processing.

    /scam_apply <n>  — move nth flagged job to clean queue
    """
    db: ApplicationDB = context.bot_data["db"]

    if not context.args:
        await update.message.reply_text("Usage: /scam_apply <n> — approve nth flagged job from /scams")
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a number, e.g. /scam_apply 1")
        return

    flagged = await db.get_flagged_queue()
    if not flagged:
        await update.message.reply_text("No flagged jobs in queue.")
        return

    if index < 1 or index > len(flagged):
        await update.message.reply_text(f"Index out of range. Use /scams to see the list (1–{len(flagged)}).")
        return

    job = flagged[index - 1]
    await db.clear_scam_flag(job.id)
    await update.message.reply_text(
        f"Approved: {job.title} @ {job.company}\n"
        f"Moved to normal queue — it will be processed next cycle."
    )


@requires_auth
async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a URL that was blocked by the scam detector.

    /force <url>
    """

    if not context.args:
        await update.message.reply_text("Usage: /force <url>")
        return

    url = context.args[0]
    db: ApplicationDB = context.bot_data["db"]

    # Verify the URL was actually rejected
    rejected = await db.get_rejected_jobs(limit=200)
    was_rejected = any(r["url"] == url for r in rejected)
    if not was_rejected:
        await update.message.reply_text(
            "That URL was not found in the rejected list. "
            "If it was just blocked as a direct message, send the URL directly and use /force if blocked."
        )
        return

    await update.message.reply_text(f"Forcing processing of blocked URL...")
    await _handle_job_url(update, context, url, skip_scam_check=True)


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


# ---------------------------------------------------------------------------
# /linkedin — audit the user's LinkedIn profile
# ---------------------------------------------------------------------------


@requires_auth
async def cmd_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Audit the user's LinkedIn profile and give scored feedback.

    /linkedin [<profile_url>]
      - If a URL is provided it's used directly.
      - Otherwise falls back to profile.yaml links.linkedin.
      - Requires LinkedIn auth state (data/linkedin_auth.json).
    """

    profile: dict = context.bot_data["profile"]
    args = context.args or []

    # Resolve the profile URL
    profile_url = args[0].strip() if args else ""
    if not profile_url:
        profile_url = (profile.get("links") or {}).get("linkedin", "")
    if not profile_url:
        await update.message.reply_text(
            "No LinkedIn URL found.\n\n"
            "Usage: /linkedin <your_profile_url>\n"
            "Or add your URL to profile.yaml under links.linkedin and run /linkedin."
        )
        return

    if "linkedin.com/in/" not in profile_url and "linkedin.com/pub/" not in profile_url:
        await update.message.reply_text(
            "That doesn't look like a LinkedIn profile URL.\n"
            "Expected: linkedin.com/in/<username>"
        )
        return

    await update.message.reply_text(
        "Fetching your LinkedIn profile and running the audit...\n"
        "(This takes ~20 seconds)"
    )

    auth_state = context.bot_data.get("linkedin_auth", "data/linkedin_auth.json")

    try:
        from bot.linkedin_audit import run_linkedin_audit, format_audit_report
        report = await run_linkedin_audit(profile_url, profile, auth_state)
    except Exception as e:
        logger.error("linkedin audit failed: %s", e)
        await update.message.reply_text(
            f"Audit failed: {e}\n\n"
            "Make sure you've run the LinkedIn login setup:\n"
            "  DISPLAY=:0 .venv/bin/python setup/linkedin_login.py"
        )
        return

    # Send as plain text — LLM-generated suggestions/verdicts can contain stray
    # `_`, `*`, or backticks that would crash Markdown parsing with BadRequest.
    formatted = format_audit_report(report)
    await reply_chunked(update.message, formatted)


# ---------------------------------------------------------------------------
# /website — generate a GitHub Pages personal site
# ---------------------------------------------------------------------------


@requires_auth
async def cmd_website(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a personal website from your profile and send deploy instructions.

    /website [theme]   — theme: minimal (default) | dark | academic
    /website guide     — show deployment instructions only (no file generated)
    """

    profile: dict = context.bot_data["profile"]
    args = context.args or []

    # /website guide — deployment instructions only
    if args and args[0].lower() == "guide":
        from bot.website import deployment_guide
        guide = deployment_guide(profile)
        await update.message.reply_text(guide)
        return

    # Resolve theme
    valid_themes = {"minimal", "dark", "academic"}
    theme = "minimal"
    if args and args[0].lower() in valid_themes:
        theme = args[0].lower()
    elif args and args[0].lower() not in ("guide",):
        await update.message.reply_text(
            "Unknown theme. Available themes: minimal, dark, academic\n\n"
            "Usage:\n"
            "/website           — generate with minimal theme\n"
            "/website dark      — dark theme (great for engineers)\n"
            "/website academic  — academic/research theme\n"
            "/website guide     — deployment instructions only"
        )
        return

    await update.message.reply_text(f"Generating your {theme} site...")

    from bot.website import generate_website, deployment_guide
    import tempfile, os

    html_content = generate_website(profile, theme=theme)

    # Write to a temp file and send as document
    name_slug = (profile.get("name") or "portfolio").lower().replace(" ", "-")
    filename = f"{name_slug}-site.html"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(html_content)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=(
                    f"Your {theme} portfolio site — {len(html_content):,} bytes.\n\n"
                    "Self-contained: no build tools needed.\n"
                    "Use /website guide for deploy steps."
                ),
            )
    except Exception as e:
        logger.error("website: failed to send file: %s", e)
        await update.message.reply_text(
            f"Could not send the file: {e}\n"
            f"The file was written to: {tmp_path}"
        )
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Follow up with the guide
    guide = deployment_guide(profile)
    await update.message.reply_text(guide)
