"""Pre-fill and post-submit verification for every application.

Three stages guard every submission:

  1. Before filling   — detect_job_closed, detect_already_applied, detect_captcha
  2. Before submit    — missing_required_fields / classify_missing, scan_form_errors
  3. After submit     — detect_submission_result

The goal: never submit a broken application and never assume success without
checking the page.  Each function is side-effect-free (read-only on the page)
so it can also be called in tests with a mock page object.
"""
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.models import FormField

logger = logging.getLogger(__name__)

# ── Patterns ─────────────────────────────────────────────────────────────────────

_CLOSED_PATTERNS = [
    r"no longer accepting applications?",
    r"position (has been|is) (filled|closed)",
    r"job (is )?no longer available",
    r"this job has expired",
    r"applications? (are )?closed",
    r"job listing (has been )?removed",
    r"this posting has been closed",
    r"the role has been filled",
    r"this position is no longer open",
]

_ALREADY_APPLIED_PATTERNS = [
    r"you (have )?already applied",
    r"you applied \d+",
    r"duplicate application",
]

_SUCCESS_PATTERNS = [
    r"application (has been )?submitted",
    r"thank you for (your )?appl(ying|ication)",
    r"we('ve| have) received your application",
    r"application (was )?received",
    r"your application (was )?sent",
    r"successfully applied",
    r"application complete",
]

_SUCCESS_URL_FRAGMENTS = [
    "confirmation", "thank-you", "thankyou", "submitted", "/success",
]

_CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".cf-challenge-form",
    "#challenge-form",
    "[data-sitekey]",
    ".g-recaptcha",
    ".h-captcha",
]

_ERROR_SELECTORS = [
    "[role='alert']",
    ".artdeco-inline-feedback--error",
    ".error-message",
    ".field-error",
    ".form-error",
    ".validation-error",
    "[data-error]",
    ".alert-error",
    ".input-error",
    ".inline-error",
]

# Labels whose absence is a hard stop — submitting without these is useless
_BLOCKING_KEYWORDS = frozenset({
    "email", "e-mail",
    "name", "first name", "last name", "full name",
    "resume", "cv", "curriculum vitae",
})


# ── Stage 1: before filling ───────────────────────────────────────────────────────

async def detect_job_closed(page) -> tuple[bool, str]:
    """Return (is_closed, reason) by scanning page text for closed-job signals."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        return False, ""

    for pattern in _CLOSED_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return True, m.group(0)
    return False, ""


async def detect_already_applied(page) -> bool:
    """Return True if the page indicates this job was already applied to."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        return False

    for pattern in _ALREADY_APPLIED_PATTERNS:
        if re.search(pattern, text):
            return True

    # LinkedIn "Applied" badge on the apply button
    try:
        applied_indicators = page.locator(
            "button.jobs-apply-button--applied, "
            "[aria-label='Applied'], "
            ".jobs-apply-button[disabled]"
        )
        if await applied_indicators.count() > 0:
            return True
    except Exception:
        pass

    return False


async def detect_captcha(page) -> bool:
    """Return True if a bot-challenge widget is blocking the page."""
    for selector in _CAPTCHA_SELECTORS:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


# ── Stage 2: before submit ────────────────────────────────────────────────────────

def missing_required_fields(
    fields: "list[FormField]",
    submitted: "dict[str, str]",
) -> list[str]:
    """Return labels of required fields that had answers but were not submitted.

    We only flag fields where:
      - field.required is True
      - field.answer is non-empty (we had something to fill)
      - the field label does NOT appear in the submitted dict

    This avoids false alarms for optional fields or fields the LLM couldn't answer.
    """
    submitted_lower = {k.lower() for k in submitted}
    return [
        f.label
        for f in fields
        if f.required and f.answer and f.label.lower() not in submitted_lower
    ]


def classify_missing(missing: list[str]) -> tuple[list[str], list[str]]:
    """Split missing field labels into (blocking, warnings).

    Blocking fields (email, name, resume) mean we should NOT submit — the
    application is meaningless without them.  Everything else is a warning.

    Returns:
        blocking: Fields whose absence should prevent submission.
        warnings: Fields that were missed but submission can proceed.
    """
    blocking, warnings = [], []
    for label in missing:
        lower = label.lower()
        if any(kw in lower for kw in _BLOCKING_KEYWORDS):
            blocking.append(label)
        else:
            warnings.append(label)
    return blocking, warnings


async def scan_form_errors(page) -> list[str]:
    """Collect visible form-validation error messages currently on the page.

    Returns up to 10 non-empty error strings (each capped at 200 chars).
    An empty list means no errors found — not that none exist.
    """
    errors: list[str] = []
    for sel in _ERROR_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                text = (await el.inner_text()).strip()
                if text and text[:200] not in errors:
                    errors.append(text[:200])
                if len(errors) >= 10:
                    return errors
        except Exception:
            continue
    return errors


# ── Stage 3: after submit ─────────────────────────────────────────────────────────

async def detect_submission_result(page) -> tuple[str, str]:
    """Determine whether a submission succeeded, errored, or is unknown.

    Checks URL fragments first (most reliable), then page text for success
    phrases, then error selectors.

    Returns:
        ("success", detail) — positive confirmation found.
        ("error",   detail) — error state detected.
        ("unknown", "")     — neither signal found; caller should treat as
                              unconfirmed and show screenshot to user.
    """
    # URL-based confirmation (Greenhouse, Lever often redirect to /confirmation)
    try:
        url = page.url.lower()
        for fragment in _SUCCESS_URL_FRAGMENTS:
            if fragment in url:
                return "success", f"Confirmation URL detected: {page.url}"
    except Exception:
        pass

    # Text-based success (LinkedIn modal says "Your application was sent")
    try:
        text = (await page.inner_text("body")).lower()
        for pattern in _SUCCESS_PATTERNS:
            m = re.search(pattern, text)
            if m:
                return "success", m.group(0)
    except Exception:
        pass

    # Error signals
    errors = await scan_form_errors(page)
    if errors:
        logger.warning("Post-submit error signals: %s", errors[:3])
        return "error", "; ".join(errors[:3])

    return "unknown", ""
