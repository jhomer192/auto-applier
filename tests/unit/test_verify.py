"""Tests for bot/verify.py — pre-fill and post-submit verification."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _page_with_text(text: str) -> MagicMock:
    page = MagicMock()
    page.inner_text = AsyncMock(return_value=text)
    page.url = "https://example.com/jobs/123"
    page.locator = MagicMock(return_value=MagicMock(count=AsyncMock(return_value=0)))
    page.query_selector_all = AsyncMock(return_value=[])
    return page


def _field(label: str, required: bool = True, answer: str = "yes", field_type: str = "text") -> FormField:
    return FormField(
        label=label,
        field_type=field_type,
        required=required,
        selector=f"input[name='{label.lower()}']",
        answer=answer,
    )


# ── detect_job_closed ─────────────────────────────────────────────────────────────

class TestDetectJobClosed:
    @pytest.mark.asyncio
    async def test_open_job_returns_false(self):
        page = _page_with_text("Apply now! Great opportunity at Acme Corp.")
        closed, reason = await detect_job_closed(page)
        assert not closed
        assert reason == ""

    @pytest.mark.asyncio
    async def test_detects_no_longer_accepting(self):
        page = _page_with_text("Sorry, we are no longer accepting applications for this role.")
        closed, reason = await detect_job_closed(page)
        assert closed
        assert "no longer accepting" in reason

    @pytest.mark.asyncio
    async def test_detects_position_filled(self):
        page = _page_with_text("This position has been filled. Thank you for your interest.")
        closed, reason = await detect_job_closed(page)
        assert closed

    @pytest.mark.asyncio
    async def test_detects_job_no_longer_available(self):
        page = _page_with_text("Job is no longer available.")
        closed, _ = await detect_job_closed(page)
        assert closed

    @pytest.mark.asyncio
    async def test_detects_applications_closed(self):
        page = _page_with_text("Applications are closed for this posting.")
        closed, _ = await detect_job_closed(page)
        assert closed

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self):
        page = _page_with_text("NO LONGER ACCEPTING APPLICATIONS.")
        closed, _ = await detect_job_closed(page)
        assert closed

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        page = MagicMock()
        page.inner_text = AsyncMock(side_effect=Exception("network error"))
        closed, reason = await detect_job_closed(page)
        assert not closed
        assert reason == ""


# ── detect_already_applied ────────────────────────────────────────────────────────

class TestDetectAlreadyApplied:
    @pytest.mark.asyncio
    async def test_fresh_job_returns_false(self):
        page = _page_with_text("Easy Apply — 47 applicants")
        result = await detect_already_applied(page)
        assert not result

    @pytest.mark.asyncio
    async def test_detects_you_already_applied(self):
        page = _page_with_text("You have already applied to this job.")
        result = await detect_already_applied(page)
        assert result

    @pytest.mark.asyncio
    async def test_detects_you_applied_n_days(self):
        page = _page_with_text("You applied 3 days ago")
        result = await detect_already_applied(page)
        assert result

    @pytest.mark.asyncio
    async def test_detects_duplicate_application(self):
        page = _page_with_text("Duplicate application detected.")
        result = await detect_already_applied(page)
        assert result

    @pytest.mark.asyncio
    async def test_detects_linkedin_applied_badge(self):
        page = _page_with_text("Software Engineer at Acme")
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=1)
        page.locator = MagicMock(return_value=locator_mock)
        result = await detect_already_applied(page)
        assert result

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        page = MagicMock()
        page.inner_text = AsyncMock(side_effect=Exception("timeout"))
        result = await detect_already_applied(page)
        assert not result


# ── detect_captcha ────────────────────────────────────────────────────────────────

class TestDetectCaptcha:
    @pytest.mark.asyncio
    async def test_no_captcha_returns_false(self):
        page = MagicMock()
        page.locator = MagicMock(return_value=MagicMock(count=AsyncMock(return_value=0)))
        result = await detect_captcha(page)
        assert not result

    @pytest.mark.asyncio
    async def test_detects_recaptcha_iframe(self):
        def locator_for(selector):
            mock = MagicMock()
            mock.count = AsyncMock(return_value=1 if "recaptcha" in selector else 0)
            return mock

        page = MagicMock()
        page.locator = MagicMock(side_effect=locator_for)
        result = await detect_captcha(page)
        assert result

    @pytest.mark.asyncio
    async def test_detects_cloudflare_challenge(self):
        def locator_for(selector):
            mock = MagicMock()
            mock.count = AsyncMock(return_value=1 if "cf-challenge" in selector else 0)
            return mock

        page = MagicMock()
        page.locator = MagicMock(side_effect=locator_for)
        result = await detect_captcha(page)
        assert result

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        page = MagicMock()
        page.locator = MagicMock(side_effect=Exception("error"))
        result = await detect_captcha(page)
        assert not result


# ── missing_required_fields ───────────────────────────────────────────────────────

class TestMissingRequiredFields:
    def test_no_fields_returns_empty(self):
        result = missing_required_fields([], {})
        assert result == []

    def test_all_required_fields_submitted_returns_empty(self):
        fields = [
            _field("Email", required=True, answer="jack@example.com"),
            _field("First Name", required=True, answer="Jack"),
        ]
        submitted = {"Email": "jack@example.com", "First Name": "Jack"}
        result = missing_required_fields(fields, submitted)
        assert result == []

    def test_missing_required_field_detected(self):
        fields = [
            _field("Email", required=True, answer="jack@example.com"),
            _field("Resume", required=True, answer="/tmp/resume.pdf", field_type="file"),
        ]
        submitted = {"Email": "jack@example.com"}  # Resume not submitted
        result = missing_required_fields(fields, submitted)
        assert "Resume" in result

    def test_optional_field_not_submitted_ignored(self):
        fields = [
            _field("LinkedIn", required=False, answer="https://linkedin.com/in/jack"),
        ]
        submitted = {}
        result = missing_required_fields(fields, submitted)
        assert result == []

    def test_required_field_with_empty_answer_not_flagged(self):
        # If the LLM couldn't answer the field, we shouldn't flag it as missing
        fields = [_field("Visa Status", required=True, answer="")]
        submitted = {}
        result = missing_required_fields(fields, submitted)
        assert result == []

    def test_label_matching_is_case_insensitive(self):
        fields = [_field("First Name", required=True, answer="Jack")]
        submitted = {"first name": "Jack"}  # lowercase key
        result = missing_required_fields(fields, submitted)
        assert result == []

    def test_multiple_missing_fields_all_returned(self):
        fields = [
            _field("Email", required=True, answer="jack@example.com"),
            _field("First Name", required=True, answer="Jack"),
            _field("Resume", required=True, answer="/tmp/r.pdf", field_type="file"),
        ]
        submitted = {}
        result = missing_required_fields(fields, submitted)
        assert len(result) == 3


# ── classify_missing ──────────────────────────────────────────────────────────────

class TestClassifyMissing:
    def test_email_is_blocking(self):
        blocking, _ = classify_missing(["Email"])
        assert "Email" in blocking

    def test_name_is_blocking(self):
        blocking, _ = classify_missing(["First Name", "Last Name", "Full Name"])
        assert len(blocking) == 3

    def test_resume_is_blocking(self):
        blocking, _ = classify_missing(["Resume"])
        assert "Resume" in blocking

    def test_cv_is_blocking(self):
        blocking, _ = classify_missing(["CV"])
        assert "CV" in blocking

    def test_linkedin_is_warning(self):
        _, warnings = classify_missing(["LinkedIn Profile"])
        assert "LinkedIn Profile" in warnings

    def test_phone_is_warning(self):
        _, warnings = classify_missing(["Phone Number"])
        assert "Phone Number" in warnings

    def test_mixed_fields_correctly_split(self):
        missing = ["Email", "LinkedIn", "Resume", "Years of Experience"]
        blocking, warnings = classify_missing(missing)
        assert "Email" in blocking
        assert "Resume" in blocking
        assert "LinkedIn" in warnings
        assert "Years of Experience" in warnings

    def test_empty_returns_empty_lists(self):
        blocking, warnings = classify_missing([])
        assert blocking == []
        assert warnings == []


# ── scan_form_errors ──────────────────────────────────────────────────────────────

class TestScanFormErrors:
    @pytest.mark.asyncio
    async def test_no_errors_returns_empty(self):
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[])
        result = await scan_form_errors(page)
        assert result == []

    @pytest.mark.asyncio
    async def test_collects_error_text(self):
        error_el = MagicMock()
        error_el.inner_text = AsyncMock(return_value="This field is required")

        page = MagicMock()

        call_count = [0]

        async def mock_qsa(sel):
            call_count[0] += 1
            # Return error element on first selector hit
            if call_count[0] == 1:
                return [error_el]
            return []

        page.query_selector_all = mock_qsa
        result = await scan_form_errors(page)
        assert "This field is required" in result

    @pytest.mark.asyncio
    async def test_deduplicates_identical_errors(self):
        error_el = MagicMock()
        error_el.inner_text = AsyncMock(return_value="Required")

        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[error_el, error_el])
        result = await scan_form_errors(page)
        assert result.count("Required") == 1

    @pytest.mark.asyncio
    async def test_caps_at_10_errors(self):
        els = []
        for i in range(20):
            el = MagicMock()
            el.inner_text = AsyncMock(return_value=f"Error {i}")
            els.append(el)

        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=els)
        result = await scan_form_errors(page)
        assert len(result) <= 10

    @pytest.mark.asyncio
    async def test_ignores_empty_error_elements(self):
        blank_el = MagicMock()
        blank_el.inner_text = AsyncMock(return_value="   ")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[blank_el])
        result = await scan_form_errors(page)
        assert result == []


# ── detect_submission_result ──────────────────────────────────────────────────────

class TestDetectSubmissionResult:
    @pytest.mark.asyncio
    async def test_confirmation_url_returns_success(self):
        page = _page_with_text("Thank you for applying!")
        page.url = "https://boards.greenhouse.io/acme/jobs/123/confirmation"
        outcome, detail = await detect_submission_result(page)
        assert outcome == "success"
        assert "confirmation" in detail.lower()

    @pytest.mark.asyncio
    async def test_thank_you_url_returns_success(self):
        page = _page_with_text("We got your application.")
        page.url = "https://jobs.lever.co/acme/thank-you"
        outcome, _ = await detect_submission_result(page)
        assert outcome == "success"

    @pytest.mark.asyncio
    async def test_success_text_returns_success(self):
        page = _page_with_text("Your application has been submitted. We'll be in touch.")
        page.url = "https://example.com/jobs/123"
        outcome, detail = await detect_submission_result(page)
        assert outcome == "success"

    @pytest.mark.asyncio
    async def test_thank_you_text_returns_success(self):
        page = _page_with_text("Thank you for applying to this position.")
        page.url = "https://example.com/jobs/123"
        outcome, _ = await detect_submission_result(page)
        assert outcome == "success"

    @pytest.mark.asyncio
    async def test_application_was_sent_returns_success(self):
        page = _page_with_text("Your application was sent to Acme Corp.")
        page.url = "https://linkedin.com/jobs/view/123"
        outcome, _ = await detect_submission_result(page)
        assert outcome == "success"

    @pytest.mark.asyncio
    async def test_error_state_returns_error(self):
        page = MagicMock()
        page.url = "https://example.com/jobs/123/apply"
        page.inner_text = AsyncMock(return_value="Fill in the form below")

        error_el = MagicMock()
        error_el.inner_text = AsyncMock(return_value="Please fill in all required fields")

        call_count = [0]

        async def mock_qsa(sel):
            call_count[0] += 1
            return [error_el] if call_count[0] == 1 else []

        page.query_selector_all = mock_qsa
        outcome, detail = await detect_submission_result(page)
        assert outcome == "error"
        assert "required" in detail.lower()

    @pytest.mark.asyncio
    async def test_ambiguous_page_returns_unknown(self):
        page = _page_with_text("Please review your application details below.")
        page.url = "https://example.com/jobs/123/review"
        page.query_selector_all = AsyncMock(return_value=[])
        outcome, detail = await detect_submission_result(page)
        assert outcome == "unknown"
        assert detail == ""

    @pytest.mark.asyncio
    async def test_exception_on_inner_text_falls_through(self):
        page = MagicMock()
        page.url = "https://example.com/jobs/done"
        page.inner_text = AsyncMock(side_effect=Exception("timeout"))
        page.query_selector_all = AsyncMock(return_value=[])
        # Should not raise; URL doesn't contain success fragment either
        outcome, _ = await detect_submission_result(page)
        assert outcome in ("success", "unknown")
