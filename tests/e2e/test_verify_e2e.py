"""E2E tests for bot/verify.py — detection functions in a real Chromium browser.

Covers all three verification stages:
  Stage 1: detect_job_closed, detect_already_applied, detect_captcha
  Stage 2: scan_form_errors, missing_required_fields
  Stage 3: detect_submission_result
"""
import pytest
from bot.verify import (
    classify_missing,
    detect_already_applied,
    detect_captcha,
    detect_job_closed,
    detect_submission_result,
    missing_required_fields,
    scan_form_errors,
)
from bot.models import FormField

pytestmark = pytest.mark.asyncio


# ── Stage 1: detect_job_closed ────────────────────────────────────────────────

async def test_job_closed_on_closed_page(http_server, page):
    await page.goto(f"{http_server}/greenhouse_closed.html")
    closed, reason = await detect_job_closed(page)
    assert closed is True
    assert reason  # should have a reason string


async def test_job_closed_on_lever_closed_page(http_server, page):
    await page.goto(f"{http_server}/lever_closed.html")
    closed, reason = await detect_job_closed(page)
    assert closed is True


async def test_job_not_closed_on_open_page(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    closed, _ = await detect_job_closed(page)
    assert closed is False


async def test_job_not_closed_on_lever_job(http_server, page):
    await page.goto(f"{http_server}/lever_job.html")
    closed, _ = await detect_job_closed(page)
    assert closed is False


async def test_job_not_closed_on_success_page(http_server, page):
    """Success page is not a closed job — it's a confirmation."""
    await page.goto(f"{http_server}/greenhouse_success.html")
    closed, _ = await detect_job_closed(page)
    assert closed is False


# ── Stage 1: detect_already_applied ──────────────────────────────────────────

async def test_already_applied_on_applied_page(http_server, page):
    await page.goto(f"{http_server}/greenhouse_applied.html")
    result = await detect_already_applied(page)
    assert result is True


async def test_not_already_applied_on_open_form(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    result = await detect_already_applied(page)
    assert result is False


async def test_not_already_applied_on_lever_job(http_server, page):
    await page.goto(f"{http_server}/lever_job.html")
    result = await detect_already_applied(page)
    assert result is False


# ── Stage 1: detect_captcha ───────────────────────────────────────────────────

async def test_captcha_detected_on_captcha_page(http_server, page):
    await page.goto(f"{http_server}/greenhouse_captcha.html")
    result = await detect_captcha(page)
    assert result is True


async def test_no_captcha_on_normal_form(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    result = await detect_captcha(page)
    assert result is False


async def test_no_captcha_on_lever_job(http_server, page):
    await page.goto(f"{http_server}/lever_job.html")
    result = await detect_captcha(page)
    assert result is False


# ── Stage 2: scan_form_errors ─────────────────────────────────────────────────

async def test_scan_form_errors_finds_errors(http_server, page):
    await page.goto(f"{http_server}/form_errors.html")
    errors = await scan_form_errors(page)
    assert errors, "Should detect visible error messages"
    combined = " ".join(errors).lower()
    assert "email" in combined or "required" in combined or "phone" in combined


async def test_scan_form_errors_empty_on_clean_form(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    errors = await scan_form_errors(page)
    # Fresh form with no error classes present — should return []
    assert errors == []


# ── Stage 3: detect_submission_result ────────────────────────────────────────

async def test_submission_result_success_on_thank_you_page(http_server, page):
    await page.goto(f"{http_server}/greenhouse_success.html")
    outcome, detail = await detect_submission_result(page)
    assert outcome == "success"


async def test_submission_result_success_on_lever_success(http_server, page):
    await page.goto(f"{http_server}/lever_success.html")
    outcome, detail = await detect_submission_result(page)
    assert outcome == "success"


async def test_submission_result_unknown_on_open_form(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    outcome, detail = await detect_submission_result(page)
    # An unfilled form doesn't look like success or a posted error
    assert outcome in ("unknown", "error")


async def test_submission_result_error_on_error_page(http_server, page):
    await page.goto(f"{http_server}/form_errors.html")
    outcome, detail = await detect_submission_result(page)
    # Error messages present — should report error or unknown
    assert outcome in ("error", "unknown")


# ── missing_required_fields (pure logic — no browser needed) ─────────────────

def _field(label, required=True, answer="filled"):
    return FormField(label=label, field_type="text", required=required,
                     selector=f"#{label}", answer=answer)


def test_missing_required_when_not_submitted():
    """Required field with an answer that never appeared in submitted_fields is flagged."""
    fields = [_field("Email"), _field("Phone", required=False)]
    submitted = {}
    missing = missing_required_fields(fields, submitted)
    assert "Email" in missing


def test_no_missing_when_all_required_submitted():
    fields = [_field("Email"), _field("Name")]
    submitted = {"Email": "a@b.com", "Name": "Alice"}
    missing = missing_required_fields(fields, submitted)
    assert missing == []


def test_optional_fields_not_in_missing():
    fields = [_field("Email"), _field("Cover Letter", required=False)]
    submitted = {"Email": "a@b.com"}
    missing = missing_required_fields(fields, submitted)
    assert "Cover Letter" not in missing


def test_missing_unanswered_required_not_flagged():
    """Required fields with no answer are not flagged — they were never fillable."""
    fields = [_field("Email", answer=None), _field("Name", answer="")]
    submitted = {}
    missing = missing_required_fields(fields, submitted)
    assert missing == []


def test_classify_missing_blocks_on_email():
    blocking, warnings = classify_missing(["Email", "LinkedIn"])
    assert "Email" in blocking
    assert "LinkedIn" in warnings


def test_classify_missing_blocks_on_resume():
    blocking, _ = classify_missing(["Resume", "Phone"])
    assert "Resume" in blocking


def test_classify_missing_empty_is_empty():
    blocking, warnings = classify_missing([])
    assert blocking == []
    assert warnings == []
