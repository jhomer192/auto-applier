import json
import logging
import re
from datetime import datetime, timezone

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
from bot.inbox import classify_email, GmailInbox
from bot.llm import analyze_job, claude_call, generate_cover_letter, generate_field_answer, LLMError
from bot.models import ApplicationRecord, EmailThread, PendingJob
from bot.scraper import field_answer_hint

logger = logging.getLogger(__name__)

# Conversation state keys stored in context.user_data
PENDING_JOB = "pending_job"
AWAITING_FIELD = "awaiting_field"
AWAITING_EMAIL_REPLY = "awaiting_email_reply"  # value: EmailThread

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
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._profile = profile
        self._registry = registry
        self._screenshot_dir = screenshot_dir
        self._gmail_inbox = gmail_inbox

    def build_app(self) -> Application:
        app = Application.builder().token(self._token).build()

        # Store refs in bot_data for handler access
        app.bot_data["db"] = self._db
        app.bot_data["profile"] = self._profile
        app.bot_data["registry"] = self._registry
        app.bot_data["authorized_user_id"] = self._chat_id
        app.bot_data["screenshot_dir"] = self._screenshot_dir
        app.bot_data["gmail_inbox"] = self._gmail_inbox

        app.add_handler(CommandHandler("start", cmd_help))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("history", cmd_history))
        app.add_handler(CommandHandler("cancel", cmd_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        return app

    def run(self) -> None:
        app = self.build_app()
        logger.info("Bot starting...")
        app.run_polling(drop_pending_updates=True)


def _auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True only if message is from the authorized chat."""
    return update.effective_user and update.effective_user.id == context.bot_data["authorized_user_id"]


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return
    await update.message.reply_text(
        "Auto Job Applier\n\n"
        "Send a job URL to apply:\n"
        "  \u2022 linkedin.com/jobs/view/...\n"
        "  \u2022 boards.greenhouse.io/...\n"
        "  \u2022 jobs.lever.co/...\n\n"
        "Commands:\n"
        "/status \u2014 application counts\n"
        "/history [N] \u2014 last N applications (default 10)\n"
        "/cancel \u2014 cancel pending application\n"
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
    context.user_data.pop(PENDING_JOB, None)
    context.user_data.pop(AWAITING_FIELD, None)
    context.bot_data.pop(AWAITING_EMAIL_REPLY, None)
    await update.message.reply_text("Cancelled.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update, context):
        return

    text = update.message.text.strip()
    db: ApplicationDB = context.bot_data["db"]
    profile: dict = context.bot_data["profile"]
    registry: AdapterRegistry = context.bot_data["registry"]

    # Case 1a: waiting for a recruiter email reply from the user
    # (stored in bot_data because background tasks can't access user_data)
    if context.bot_data.get(AWAITING_EMAIL_REPLY):
        await _handle_email_reply(update, context, text)
        return

    # Case 1b: we are waiting for a field answer from the user
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
        else:
            await update.message.reply_text("Reply Y to apply or N to skip. Or /cancel.")
        return

    # Case 3: new job URL
    if _is_job_url(text):
        await _handle_job_url(update, context, text)
        return

    # Case 4: unrecognized
    await update.message.reply_text(
        "Send me a job URL (LinkedIn Easy Apply, Greenhouse, or Lever) to get started.\n/help for commands."
    )


async def _handle_job_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    registry: AdapterRegistry = context.bot_data["registry"]
    adapter = registry.get(url)

    if not adapter:
        await update.message.reply_text(
            "Unsupported site. I can apply on:\n"
            "\u2022 linkedin.com/jobs/view/...\n"
            "\u2022 boards.greenhouse.io/...\n"
            "\u2022 jobs.lever.co/..."
        )
        return

    await update.message.reply_text("Fetching job info...")

    try:
        job_info = await adapter.fetch_job_info(url)
    except Exception as e:
        await update.message.reply_text(f"Could not fetch that URL: {e}")
        return

    fields = await adapter.extract_fields(url)
    pending = PendingJob(url=url, job_info=job_info, fields=fields)
    context.user_data[PENDING_JOB] = pending

    await update.message.reply_text(
        f"*{job_info.title}* at *{job_info.company}*\n\nApply? Reply Y to apply, N to skip.",
        parse_mode="Markdown",
    )


async def _proceed_with_application(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingJob,
) -> None:
    profile: dict = context.bot_data["profile"]
    registry: AdapterRegistry = context.bot_data["registry"]
    db: ApplicationDB = context.bot_data["db"]

    await update.message.reply_text("Analyzing job and generating answers...")

    # Analyze job
    try:
        job_analysis = await analyze_job(pending.job_info.raw_html, profile)
    except LLMError as e:
        await update.message.reply_text(f"LLM error during analysis: {e}")
        return

    # Generate field answers
    needs_user_input: list[int] = []
    for i, form_field in enumerate(pending.fields):
        if form_field.field_type == "file":
            form_field.answer = profile.get("resume_path", "")
            continue
        try:
            hint = field_answer_hint(form_field)
            answer = await generate_field_answer(
                form_field.label, f"Job: {pending.job_info.title}", profile, job_analysis,
                field_hint=hint,
            )
            if answer.startswith("NEEDS_USER_INPUT:"):
                needs_user_input.append(i)
            else:
                form_field.answer = answer
        except LLMError:
            needs_user_input.append(i)

    # Generate cover letter and attach to first cover letter field
    cover_field_idx = next(
        (i for i, f in enumerate(pending.fields) if "cover" in f.label.lower()), None
    )
    if cover_field_idx is not None and pending.fields[cover_field_idx].answer == "":
        try:
            cl = await generate_cover_letter(job_analysis, profile)
            pending.fields[cover_field_idx].answer = cl
            if cover_field_idx in needs_user_input:
                needs_user_input.remove(cover_field_idx)
        except LLMError:
            pass

    if needs_user_input:
        # Store which fields still need user input
        pending.awaiting_fields = [pending.fields[i] for i in needs_user_input]
        pending.current_field_index = 0
        context.user_data[PENDING_JOB] = pending
        context.user_data[AWAITING_FIELD] = True

        form_field = pending.awaiting_fields[0]
        await update.message.reply_text(
            f"I need a few answers before I can apply.\n\n"
            f"*{form_field.label}*: This field is required but is not in your profile.\n\n"
            f"Please answer:",
            parse_mode="Markdown",
        )
        return

    # All fields resolved — submit
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
    email via Claude when the thread is an interview request."""
    thread: EmailThread = context.bot_data.pop(AWAITING_EMAIL_REPLY)
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
        await update.message.reply_text(
            f"Sent to {thread.from_address}:\n\n{body}"
        )
    except Exception as e:
        logger.error("Failed to send email reply: %s", e)
        await update.message.reply_text(f"Failed to send reply: {e}")


async def _compose_offer_reply(
    thread: EmailThread,
    user_input: str,
    profile: dict,
) -> str:
    """Use Claude CLI to write a professional offer response.

    user_input is the user's intent: accept / decline / counter with details.
    """
    name = profile.get("name", "")
    prompt = (
        f"Write a short, professional reply to a job offer email.\n\n"
        f"OFFER EMAIL:\n"
        f"From: {thread.from_address}\n"
        f"Subject: {thread.subject}\n"
        f"Message: {thread.body_preview}\n\n"
        f"CANDIDATE NAME: {name}\n\n"
        f"CANDIDATE'S INTENT:\n{user_input}\n\n"
        "Write ONLY the email body (no subject line, no headers).\n"
        "Keep it to 3-5 sentences. Be warm and professional.\n"
        "If accepting: express genuine enthusiasm and confirm any next steps.\n"
        "If declining: be gracious and keep the door open.\n"
        "If countering: state the counter clearly and professionally, "
        "framing it as a question rather than a demand.\n"
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
    prompt = (
        f"Write a short, professional reply to a recruiter interview request.\n\n"
        f"RECRUITER EMAIL:\n"
        f"From: {thread.from_address}\n"
        f"Subject: {thread.subject}\n"
        f"Message: {thread.body_preview}\n\n"
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
            # Store thread so the next free-text message is treated as the reply
            app.bot_data[AWAITING_EMAIL_REPLY] = email_thread
        except Exception as e:
            logger.error("notify_new_emails: failed to send notification: %s", e)


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

    await update.message.reply_text("Submitting application...")

    try:
        result = await adapter.submit_application(pending.url, pending.fields, resume_path)
    except Exception as e:
        await update.message.reply_text(f"Application failed: {e}\nNothing was submitted.")
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

        # Field summary (cap at 15 fields)
        field_lines = [f"  {k}: {v[:80]}" for k, v in list(result.submitted_fields.items())[:15]]
        await update.message.reply_text(
            f"Application submitted! (ID: {app_id})\n\n"
            "Submitted:\n" + "\n".join(field_lines)
        )
    else:
        await update.message.reply_text(
            f"Application failed: {result.error}\nNothing was submitted. (ID: {app_id})"
        )
