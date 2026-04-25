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
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bot.models import EmailThread

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Email classification ──────────────────────────────────────────────────────

_OFFER_SIGNALS = [
    "offer letter", "job offer", "offer of employment", "formal offer",
    "pleased to offer", "happy to offer", "excited to offer",
    "offer you the position", "offer you the role", "offer you a position",
    "compensation package", "salary offer", "start date",
    "we'd like to extend", "we would like to extend",
]

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

# Strong rejection: unambiguous multi-word phrases — fire on 1 hit anywhere
_STRONG_REJECTION_SIGNALS = {
    "not moving forward", "will not be moving", "other candidates",
    "not selected", "not a fit", "won't be moving", "position has been filled",
    "filled the position", "not be continuing", "not be proceeding",
    "regret to inform", "not be able to move",
}

# Weak rejection: single words/short phrases that can appear in positive context
_WEAK_REJECTION_SIGNALS = {
    "unfortunately", "decided to", "different direction",
}

_CONFIRMATION_SIGNALS = [
    "application received", "thank you for applying", "we received your application",
    "successfully submitted", "your application has been", "application confirmation",
    "we'll be in touch", "we will be in touch", "under review",
    "being reviewed", "will review your",
]


def classify_email(thread: EmailThread) -> str:
    """Classify an inbound email as one of:
      'offer', 'interview', 'rejection', 'confirmation', 'other'

    Uses weighted keyword matching on subject + body preview. Fast, no LLM needed.

    Thresholds prevent single-word false positives (e.g. "unfortunately your
    interview went well" should NOT be classified as a rejection).

    Priority order when tied: rejection > offer > interview > confirmation > other.
    """
    subject_lower = thread.subject.lower()
    body_lower = thread.body_preview.lower()
    # Subject matches count double — subject is a more reliable signal
    text_full = subject_lower + " " + subject_lower + " " + body_lower

    offer_hits = sum(1 for s in _OFFER_SIGNALS if s in text_full)
    interview_hits = sum(1 for s in _INTERVIEW_SIGNALS if s in text_full)
    confirmation_hits = sum(1 for s in _CONFIRMATION_SIGNALS if s in text_full)

    # Rejection: strong unambiguous phrases fire on 1 hit anywhere;
    # weak ambiguous words ("unfortunately") require a hit in the subject
    # (subject is far more reliable) or 2+ hits total across subject + body.
    strong_rejection = any(s in text_full for s in _STRONG_REJECTION_SIGNALS)
    rejection_in_subject = any(s in subject_lower for s in _REJECTION_SIGNALS)
    weak_rejection_hits = sum(1 for s in _WEAK_REJECTION_SIGNALS if s in text_full)
    if strong_rejection or rejection_in_subject or weak_rejection_hits >= 2:
        return "rejection"

    # Offer requires 1+ specific hit (offer signals are quite specific)
    if offer_hits >= 1:
        return "offer"

    # Interview requires 1+ hit (most interview signals are fairly specific)
    if interview_hits >= 1:
        return "interview"

    # Automated confirmation
    if confirmation_hits >= 1:
        return "confirmation"

    return "other"


# ── Header / body helpers ─────────────────────────────────────────────────────

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
        domain = self._address.split("@")[-1] if "@" in self._address else "mail.local"
        own_message_id = f"<{uuid.uuid4().hex}@{domain}>"

        msg = MIMEMultipart("alternative")
        msg["From"] = self._address
        msg["To"] = to_address
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Message-ID"] = own_message_id
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {own_message_id}".strip()
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
