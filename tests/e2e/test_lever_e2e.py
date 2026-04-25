"""E2E tests for LeverAdapter — full pipeline against a local mock server.

Tests cover:
  - fetch_job_info: title/company extraction from Lever job page
  - extract_fields: dynamic scraping after clicking the Apply button
  - submit_application: fill + submit + success detection
  - submit_application: closed job detection
  - submit_application: fallback field list on failed fetch
"""
import pytest
from bot.adapters.lever import LeverAdapter
from bot.models import FormField

pytestmark = pytest.mark.asyncio


@pytest.fixture
def adapter():
    return LeverAdapter()


# ── fetch_job_info ────────────────────────────────────────────────────────────

async def test_fetch_job_info_extracts_title(http_server, adapter):
    url = f"{http_server}/lever_job.html"
    job = await adapter.fetch_job_info(url)
    assert "engineer" in job.title.lower() or "backend" in job.title.lower()


async def test_fetch_job_info_derives_company_from_url(http_server, adapter):
    """Lever derives company name from URL slug. Mock server uses 127.0.0.1 so
    the slug extraction falls back gracefully."""
    url = f"{http_server}/lever_job.html"
    job = await adapter.fetch_job_info(url)
    # Company may be empty or derived — just check it doesn't crash
    assert isinstance(job.company, str)


async def test_fetch_job_info_captures_html(http_server, adapter):
    url = f"{http_server}/lever_job.html"
    job = await adapter.fetch_job_info(url)
    assert len(job.raw_html) > 100
    assert "backend" in job.raw_html.lower() or "widget" in job.raw_html.lower()


# ── extract_fields ────────────────────────────────────────────────────────────

async def test_extract_fields_returns_list(http_server, adapter):
    url = f"{http_server}/lever_job.html"
    fields = await adapter.extract_fields(url)
    assert isinstance(fields, list)
    assert len(fields) > 0


async def test_extract_fields_includes_key_fields(http_server, adapter):
    url = f"{http_server}/lever_job.html"
    fields = await adapter.extract_fields(url)
    labels = [f.label.lower() for f in fields]
    # Name and email are always expected
    assert any("name" in l for l in labels), f"name not found in {labels}"
    assert any("email" in l for l in labels), f"email not found in {labels}"


async def test_extract_fields_fallback_on_bad_url(adapter):
    """On a URL that fails to load, extract_fields should return the static fallback."""
    fields = await adapter.extract_fields("http://127.0.0.1:1/nonexistent")
    assert fields
    labels = [f.label for f in fields]
    assert "Full Name" in labels
    assert "Email" in labels


# ── submit_application ────────────────────────────────────────────────────────

async def test_submit_detects_closed_job(http_server, adapter, resume_pdf):
    url = f"{http_server}/lever_closed.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.closed is True
    assert result.success is False


async def test_submit_fills_and_detects_success(http_server, adapter, resume_pdf):
    """Happy path: fill all Lever form fields, submit, detect success."""
    url = f"{http_server}/lever_job.html"
    fields = [
        FormField(label="Full Name", field_type="text", required=True,
                  selector="#name", answer="Jane Smith"),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="jane@example.com"),
        FormField(label="Phone", field_type="text", required=False,
                  selector="#phone", answer="555-0000"),
        FormField(label="Cover Letter / Additional Info", field_type="textarea", required=False,
                  selector="#comments", answer="Excited to apply!"),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True
    assert result.submitted_fields.get("Full Name") == "Jane Smith"


async def test_submit_records_all_filled_fields(http_server, adapter, resume_pdf):
    url = f"{http_server}/lever_job.html"
    fields = [
        FormField(label="Full Name", field_type="text", required=True,
                  selector="#name", answer="Alice"),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="alice@test.com"),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert "Full Name" in result.submitted_fields
    assert "Email" in result.submitted_fields


async def test_submit_aborts_when_required_field_has_no_answer(http_server, adapter, resume_pdf):
    """Required blocking field with no answer should abort before touching the form."""
    url = f"{http_server}/lever_job.html"
    fields = [
        FormField(label="Full Name", field_type="text", required=True,
                  selector="#name", answer=""),  # no answer generated
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="test@example.com"),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is False
    assert "Full Name" in result.missing_fields


async def test_submit_skips_optional_fields_with_no_answer(http_server, adapter, resume_pdf):
    """Optional fields with empty answer are skipped; form still submits."""
    url = f"{http_server}/lever_job.html"
    fields = [
        FormField(label="Full Name", field_type="text", required=True,
                  selector="#name", answer="Jane Smith"),
        FormField(label="Email", field_type="text", required=True,
                  selector="#email", answer="jane@example.com"),
        FormField(label="Phone", field_type="text", required=False,
                  selector="#phone", answer=""),  # optional, no answer
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True
    assert "Phone" not in result.submitted_fields
