"""Gmail inbox poller and SMTP sender.

Uses only Python stdlib (imaplib, smtplib, email) — no extra dependencies.
Authentication is via Gmail App Password, so normal Gmail access is unaffected.

Setup:
  1. Enable 2FA on the Gmail account
  2. Generate an App Password at https://myaccount.google.com/apppasswords
  3. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env
"""

import asyncio
import email as email_lib
import email.header
import email.utils
import imaplib
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bot.models import EmailThread

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"

# ── Email classification ──────────────────────────────────────────────────────

_INTERVIEW_SIGNALS = [
    "interview", "schedule", "availability", "time slot", "phone screen",
    "video call", "zoom", "google meet", "teams call", "technical screen",
    "next steps", "move forward", "moving forward", "like to connect",
    "speak with you", "chat with you", "meet with you", "invite you",
    "we'd love", "we would love", "excited to", "pleased to",
]

_REJECTION_SIGNALS = [
    "unfortunately", "not moving forward", "will not be moving",
    "other candidates", "not selected", "not a fit", "won't be moving",
    "decided to", "position has been filled", "filled the position",
    "not be continuing", "not be proceeding", "regret to inform",
    "different direction", "not be able to move",
]

_CONFIRMATION_SIGNALS = [
    "application received", "thank you for applying", "we received your application",
    "successfully submitted", "your application has been", "application confirmation",
    "we'll be in touch", "we will be in touch", "under review",
    "being reviewed", "will review your",
]


def classify_email(thread: EmailThread) -> str:
    """Classify an inbound email as 'interview', 'rejection', 'confirmation', or 'other'.

    Uses keyword matching on subject + body preview. Fast, no LLM needed.
    Returns 'interview' only when signals are unambiguous — false negatives
    are better than notifying on noise.
    """
    text = (thread.subject + " " + thread.body_preview).lower()

    rejection_hits = sum(1 for s in _REJECTION_SIGNALS if s in text)
    confirmation_hits = sum(1 for s in _CONFIRMATION_SIGNALS if s in text)
    interview_hits = sum(1 for s in _INTERVIEW_SIGNALS if s in text)

    # Explicit rejection wins over everything
    if rejection_hits >= 1:
        return "rejection"

    # Automated confirmation (no human action needed)
    if confirmation_hits >= 1 and interview_hits == 0:
        return "confirmation"

    # Interview: at least one signal, not drowned out by rejections
    if interview_hits >= 1:
        return "interview"

    return "other"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _decode_header(value: str) -> str:
    """Decode an RFC 2047-encoded header value to a plain string."""
    parts = email.header.decode_header(value or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_plain_body(msg: email_lib.message.Message) -> str:
    """Walk a MIME message and return the first text/plain part, truncated to 500 chars."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")[:500]
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")[:500]
    return ""


class GmailInbox:
    def __init__(self, address: str, app_password: str) -> None:
        self._address = address
        self._app_password = app_password

    # ── IMAP polling ─────────────────────────────────────────────────────────

    def _fetch_unseen_messages(self) -> list[EmailThread]:
        """Connect via IMAP, fetch UNSEEN messages in INBOX, return as EmailThread list.

        This is a blocking call — run it in a thread via asyncio.to_thread().
        Does NOT mark messages as read (we use PEEK to leave the Seen flag intact).
        """
        threads: list[EmailThread] = []
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as conn:
                conn.login(self._address, self._app_password)
                conn.select("INBOX", readonly=True)  # readonly so we don't auto-mark seen

                _, data = conn.search(None, "UNSEEN")
                uids = data[0].split() if data and data[0] else []

                for uid in uids:
                    try:
                        # BODY.PEEK so server doesn't set \Seen
                        _, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                        if not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1]
                        msg = email_lib.message_from_bytes(raw)

                        message_id = (msg.get("Message-ID") or "").strip()
                        if not message_id:
                            continue  # malformed, skip

                        in_reply_to = (msg.get("In-Reply-To") or "").strip()
                        references = (msg.get("References") or "").strip()

                        # Thread root = first reference, or In-Reply-To, or own message_id
                        if references:
                            thread_id = references.split()[0]
                        elif in_reply_to:
                            thread_id = in_reply_to
                        else:
                            thread_id = message_id

                        from_raw = msg.get("From") or ""
                        from_address = email.utils.parseaddr(from_raw)[1] or from_raw
                        subject = _decode_header(msg.get("Subject") or "(no subject)")
                        body_preview = _extract_plain_body(msg)

                        threads.append(EmailThread(
                            message_id=message_id,
                            thread_id=thread_id,
                            from_address=from_address,
                            subject=subject,
                            body_preview=body_preview,
                            direction="inbound",
                        ))
                    except Exception as e:
                        logger.warning("inbox: error parsing message uid=%s: %s", uid, e)

        except imaplib.IMAP4.error as e:
            logger.error("inbox: IMAP error: %s", e)
        except OSError as e:
            logger.error("inbox: network error: %s", e)

        return threads

    async def poll(self) -> list[EmailThread]:
        """Async wrapper around IMAP fetch. Returns new unseen messages."""
        return await asyncio.to_thread(self._fetch_unseen_messages)

    # ── SMTP sending ──────────────────────────────────────────────────────────

    def _send_reply_sync(
        self,
        to_address: str,
        subject: str,
        body: str,
        in_reply_to: str,
        references: str,
    ) -> None:
        """Send a reply email via SMTP. Blocking — run via asyncio.to_thread()."""
        msg = MIMEMultipart("alternative")
        msg["From"] = self._address
        msg["To"] = to_address
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(self._address, self._app_password)
            server.sendmail(self._address, [to_address], msg.as_string())

    async def send_reply(self, thread: EmailThread, body: str) -> None:
        """Send a reply to a recruiter email and log it."""
        await asyncio.to_thread(
            self._send_reply_sync,
            thread.from_address,
            thread.subject,
            body,
            thread.message_id,
            f"{thread.thread_id} {thread.message_id}".strip(),
        )
        logger.info("inbox: sent reply to %s re: %s", thread.from_address, thread.subject)
