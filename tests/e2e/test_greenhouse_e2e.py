"""E2E tests for GreenhouseAdapter — full pipeline against a local mock server.

Tests cover:
  - fetch_job_info: title/company extraction
  - extract_fields: dynamic scraping of all form fields
  - submit_application: fill + submit + success detection
  - submit_application: closed job detection (Stage 1 abort)
  - submit_application: already-applied detection (Stage 1 abort)
  - submit_application: captcha detection (Stage 1 abort)
  - submit_application: missing blocking field abort (Stage 2)
"""
import pytest
from unittest.mock import patch, AsyncMock
from bot.adapters.greenhouse import GreenhouseAdapter
from bot.models import FormField

pytestmark = pytest.mark.asyncio


@pytest.fixture
def adapter():
    return GreenhouseAdapter()


# ── fetch_job_info ────────────────────────────────────────────────────────────

async def test_fetch_job_info_extracts_title(http_server, adapter):
    url = f"{http_server}/greenhouse_job.html"
    job = await adapter.fetch_job_info(url)
    assert "engineer" in job.title.lower() or "software" in job.title.lower()


async def test_fetch_job_info_extracts_company(http_server, adapter):
    url = f"{http_server}/greenhouse_job.html"
    job = await adapter.fetch_job_info(url)
    assert "acme" in job.company.lower()


async def test_fetch_job_info_captures_html(http_server, adapter):
    url = f"{http_server}/greenhouse_job.html"
    job = await adapter.fetch_job_info(url)
    assert len(job.raw_html) > 100
    assert "acme" in job.raw_html.lower()


async def test_fetch_job_info_sets_url(http_server, adapter):
    url = f"{http_server}/greenhouse_job.html"
    job = await adapter.fetch_job_info(url)
    assert job.url == url


# ── extract_fields ────────────────────────────────────────────────────────────

async def test_extract_fields_returns_list(http_server, adapter):
    url = f"{http_server}/application/greenhouse.html"
    fields = await adapter.extract_fields(url)
    assert isinstance(fields, list)
    assert len(fields) > 0


async def test_extract_fields_includes_required_fields(http_server, adapter):
    url = f"{http_server}/application/greenhouse.html"
    fields = await adapter.extract_fields(url)
    labels = [f.label.lower() for f in fields]
    for expected in ("first name", "last name", "email"):
        assert any(expected in l for l in labels), f"'{expected}' not in {labels}"


async def test_extract_fields_finds_file_field(http_server, adapter):
    url = f"{http_server}/application/greenhouse.html"
    fields = await adapter.extract_fields(url)
    file_fields = [f for f in fields if f.field_type == "file"]
    assert file_fields, "No file upload field found"


async def test_extract_fields_finds_select_field(http_server, adapter):
    url = f"{http_server}/application/greenhouse.html"
    fields = await adapter.extract_fields(url)
    selects = [f for f in fields if f.field_type == "select"]
    assert selects, "No select field found"


async def test_extract_fields_fallback_on_bad_url(adapter):
    """On a URL that fails to load, extract_fields should return the static fallback."""
    fields = await adapter.extract_fields("http://127.0.0.1:1/nonexistent")
    assert fields  # fallback is not empty
    labels = [f.label for f in fields]
    assert "First Name" in labels
    assert "Email" in labels


# ── submit_application ────────────────────────────────────────────────────────

async def test_submit_detects_closed_job(http_server, adapter, resume_pdf):
    url = f"{http_server}/application/greenhouse_closed.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.closed is True
    assert result.success is False


async def test_submit_detects_already_applied(http_server, adapter, resume_pdf):
    url = f"{http_server}/application/greenhouse_applied.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.already_applied is True
    assert result.success is False


async def test_submit_detects_captcha(http_server, adapter, resume_pdf):
    url = f"{http_server}/application/greenhouse_captcha.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.success is False
    assert "captcha" in (result.error or "").lower() or "challenge" in (result.error or "").lower()


async def test_submit_aborts_on_missing_blocking_field(http_server, adapter, resume_pdf):
    """If critical required fields have no answer, adapter should abort before submit."""
    url = f"{http_server}/application/greenhouse.html"
    # Required fields are in the list but have no answers — adapter must detect and abort
    fields = [
        FormField(label="First Name", field_type="text", required=True,
                  selector="#first_name", answer=None),
        FormField(label="Last Name", field_type="text", required=True,
                  selector="#last_name", answer=None),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer=None),
        FormField(label="LinkedIn Profile", field_type="text", required=False,
                  selector="#linkedin_profile", answer="https://linkedin.com/in/test"),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    # Required blocking fields unanswered — should abort without submitting
    assert result.success is False
    assert result.missing_fields


async def test_submit_fills_and_detects_success(http_server, adapter, resume_pdf):
    """Full happy path: fill all fields, submit, detect success page."""
    url = f"{http_server}/application/greenhouse.html"
    fields = [
        FormField(label="First Name", field_type="text", required=True,
                  selector="#first_name", answer="Jane"),
        FormField(label="Last Name", field_type="text", required=True,
                  selector="#last_name", answer="Smith"),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="jane@example.com"),
        FormField(label="Phone", field_type="text", required=False,
                  selector="#phone", answer="555-1234"),
        FormField(label="Cover Letter", field_type="textarea", required=False,
                  selector="#cover_letter", answer="I am excited to apply."),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True
    assert result.submitted_fields.get("First Name") == "Jane"
    assert result.submitted_fields.get("Email") == "jane@example.com"


async def test_submit_records_submitted_fields(http_server, adapter, resume_pdf):
    url = f"{http_server}/application/greenhouse.html"
    fields = [
        FormField(label="First Name", field_type="text", required=True,
                  selector="#first_name", answer="Bob"),
        FormField(label="Last Name", field_type="text", required=True,
                  selector="#last_name", answer="Jones"),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="bob@example.com"),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert "First Name" in result.submitted_fields
    assert "Email" in result.submitted_fields
