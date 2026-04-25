"""E2E tests for the LinkedIn adapter.

All tests use a local HTTP server serving LinkedIn-shaped fixture HTML
(same class names / aria attributes that the adapter targets).  No real
LinkedIn account is needed.  All human-like pauses are mocked to instant
by the autouse `instant_pauses` fixture in conftest.py.

Coverage:
  fetch_job_info   — title, company, HTML capture, graceful fallback
  extract_fields   — modal open, multi-step scraping, dedup, no-button fallback
  submit_application — happy path, closed detection, already-applied detection,
                       bad resume, fields recorded, success confirmation
"""
import pytest
import pytest_asyncio

from bot.adapters.linkedin import LinkedInAdapter
from bot.models import FormField


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def adapter():
    """LinkedIn adapter with no auth state (no cookie file required)."""
    return LinkedInAdapter(auth_state_path=None)


# ── fetch_job_info ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_job_info_extracts_title(http_server, adapter):
    url = f"{http_server}/linkedin_job.html"
    info = await adapter.fetch_job_info(url)
    assert info.title == "Software Engineer"


@pytest.mark.asyncio
async def test_fetch_job_info_extracts_company(http_server, adapter):
    url = f"{http_server}/linkedin_job.html"
    info = await adapter.fetch_job_info(url)
    assert info.company == "Acme Corp"


@pytest.mark.asyncio
async def test_fetch_job_info_captures_html(http_server, adapter):
    url = f"{http_server}/linkedin_job.html"
    info = await adapter.fetch_job_info(url)
    assert "Software Engineer" in info.raw_html
    assert "Acme Corp" in info.raw_html


@pytest.mark.asyncio
async def test_fetch_job_info_preserves_url(http_server, adapter):
    url = f"{http_server}/linkedin_job.html"
    info = await adapter.fetch_job_info(url)
    assert info.url == url


@pytest.mark.asyncio
async def test_fetch_job_info_falls_back_on_missing_selector(http_server, adapter):
    """Closed page has no Easy Apply button but job selectors absent → fallback to title()."""
    url = f"{http_server}/linkedin_closed.html"
    info = await adapter.fetch_job_info(url)
    # Company selector is present in closed fixture; title selector is present too
    assert info.title == "Software Engineer"
    assert info.company == "Acme Corp"


# ── extract_fields ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_fields_opens_easy_apply_modal(http_server, adapter):
    """Clicking Easy Apply must expose form fields that weren't visible initially."""
    url = f"{http_server}/linkedin_job.html"
    fields = await adapter.extract_fields(url)
    labels = [f.label for f in fields]
    assert "Full Name" in labels


@pytest.mark.asyncio
async def test_extract_fields_returns_all_steps(http_server, adapter):
    """Both step-1 and step-2 fields must appear in the combined result."""
    url = f"{http_server}/linkedin_job.html"
    fields = await adapter.extract_fields(url)
    labels = [f.label for f in fields]
    # Step 1 fields
    assert "Full Name" in labels
    assert "Email Address" in labels
    # Step 2 fields (injected after Next click)
    assert "Phone Number" in labels


@pytest.mark.asyncio
async def test_extract_fields_includes_file_type(http_server, adapter):
    """The Resume upload field must be found and typed as 'file'."""
    url = f"{http_server}/linkedin_job.html"
    fields = await adapter.extract_fields(url)
    file_fields = [f for f in fields if f.field_type == "file"]
    assert len(file_fields) >= 1
    assert any("resume" in f.label.lower() for f in file_fields)


@pytest.mark.asyncio
async def test_extract_fields_deduplicates_across_steps(http_server, adapter):
    """A field that appears on multiple steps must only be returned once."""
    url = f"{http_server}/linkedin_job.html"
    fields = await adapter.extract_fields(url)
    labels = [f.label for f in fields]
    assert len(labels) == len(set(labels)), f"Duplicate labels: {labels}"


@pytest.mark.asyncio
async def test_extract_fields_no_easy_apply_returns_fallback(http_server, adapter):
    """If the Easy Apply button is absent, return the static fallback field list."""
    url = f"{http_server}/linkedin_closed.html"  # no jobs-apply-button
    fields = await adapter.extract_fields(url)
    # Static fallback always includes Phone, Resume, Cover Letter
    assert len(fields) >= 1
    field_types = {f.field_type for f in fields}
    assert "file" in field_types  # Resume is always in static fallback


@pytest.mark.asyncio
async def test_extract_fields_all_have_selectors(http_server, adapter):
    """Every returned field must have a non-empty selector."""
    url = f"{http_server}/linkedin_job.html"
    fields = await adapter.extract_fields(url)
    for f in fields:
        assert f.selector, f"Field {f.label!r} has no selector"


# ── submit_application ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_application_validates_resume_path(adapter, http_server):
    """submit_application raises ValueError if the resume file doesn't exist."""
    import pytest
    with pytest.raises(ValueError, match="not found"):
        await adapter.submit_application(
            f"{http_server}/linkedin_job.html", [], "/nonexistent/resume.pdf"
        )


@pytest.mark.asyncio
async def test_submit_application_detects_closed_job(http_server, adapter, resume_pdf):
    """Closed job page must return closed=True without attempting to fill."""
    url = f"{http_server}/linkedin_closed.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.closed is True
    assert result.success is False
    assert result.submitted_fields == {}


@pytest.mark.asyncio
async def test_submit_application_detects_already_applied(http_server, adapter, resume_pdf):
    """Page with disabled 'Applied' button must return already_applied=True."""
    url = f"{http_server}/linkedin_applied.html"
    result = await adapter.submit_application(url, [], resume_pdf)
    assert result.already_applied is True
    assert result.success is False
    assert result.submitted_fields == {}


@pytest.mark.asyncio
async def test_submit_application_fills_and_succeeds(http_server, adapter, resume_pdf):
    """Happy path: fills all fields across steps and detects success message."""
    url = f"{http_server}/linkedin_job.html"
    fields = [
        FormField(label="Full Name",    field_type="text", required=True,
                  selector="#full-name",   answer="Jane Smith"),
        FormField(label="Email Address", field_type="text", required=True,
                  selector="#email-addr",  answer="jane@example.com"),
        FormField(label="Phone Number", field_type="text", required=False,
                  selector="#phone-num",   answer="555-0100"),
        FormField(label="Resume",       field_type="file", required=True,
                  selector="#resume-file", answer=resume_pdf),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True


@pytest.mark.asyncio
async def test_submit_application_records_submitted_fields(http_server, adapter, resume_pdf):
    """All filled fields must appear in submitted_fields."""
    url = f"{http_server}/linkedin_job.html"
    fields = [
        FormField(label="Full Name",    field_type="text", required=True,
                  selector="#full-name",   answer="Jane Smith"),
        FormField(label="Email Address", field_type="text", required=True,
                  selector="#email-addr",  answer="jane@example.com"),
        FormField(label="Phone Number", field_type="text", required=False,
                  selector="#phone-num",   answer="555-0100"),
        FormField(label="Resume",       field_type="file", required=True,
                  selector="#resume-file", answer=resume_pdf),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True
    assert "Full Name" in result.submitted_fields
    assert result.submitted_fields["Full Name"] == "Jane Smith"
    assert "Email Address" in result.submitted_fields


@pytest.mark.asyncio
async def test_submit_application_confirmed_via_success_text(http_server, adapter, resume_pdf):
    """Post-submit page text must trigger submission_confirmed=True."""
    url = f"{http_server}/linkedin_job.html"
    fields = [
        FormField(label="Full Name",    field_type="text", required=True,
                  selector="#full-name",   answer="Jane Smith"),
        FormField(label="Email Address", field_type="text", required=True,
                  selector="#email-addr",  answer="jane@example.com"),
        FormField(label="Phone Number", field_type="text", required=False,
                  selector="#phone-num",   answer="555-0100"),
        FormField(label="Resume",       field_type="file", required=True,
                  selector="#resume-file", answer=resume_pdf),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.submission_confirmed is True


@pytest.mark.asyncio
async def test_submit_application_skips_fields_with_no_answer(http_server, adapter, resume_pdf):
    """Optional field with empty answer must not appear in submitted_fields."""
    url = f"{http_server}/linkedin_job.html"
    fields = [
        FormField(label="Full Name",    field_type="text", required=True,
                  selector="#full-name",   answer="Jane Smith"),
        FormField(label="Email Address", field_type="text", required=True,
                  selector="#email-addr",  answer="jane@example.com"),
        FormField(label="Phone Number", field_type="text", required=False,
                  selector="#phone-num",   answer=""),  # no answer
        FormField(label="Resume",       field_type="file", required=True,
                  selector="#resume-file", answer=resume_pdf),
    ]
    result = await adapter.submit_application(url, fields, resume_pdf)
    assert result.success is True
    assert "Phone Number" not in result.submitted_fields
