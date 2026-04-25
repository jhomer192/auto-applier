"""E2E tests for bot/scraper.py — field extraction in a real Chromium browser.

These tests navigate to local mock HTML pages and verify that
extract_fields_from_page() correctly detects, labels, and classifies
every field type, and that noise / hidden fields are skipped.
"""
import pytest
from bot.scraper import extract_fields_from_page, is_eeo_field

pytestmark = pytest.mark.asyncio


# ── Field extraction ──────────────────────────────────────────────────────────

async def test_extracts_text_inputs(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    assert any("first name" in l for l in labels), f"first name not found in {labels}"


async def test_extracts_email_via_aria_label(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    assert any("email" in l for l in labels), f"email not found in {labels}"


async def test_extracts_textarea(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    textarea_fields = [f for f in fields if f.field_type == "textarea"]
    assert textarea_fields, "No textarea fields found"
    labels = [f.label.lower() for f in textarea_fields]
    assert any("cover letter" in l or "bio" in l for l in labels)


async def test_extracts_select_with_options(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    select_fields = [f for f in fields if f.field_type == "select"]
    assert select_fields, "No select fields found"
    # Should include the options
    exp_field = next((f for f in select_fields if "experience" in f.label.lower()), None)
    assert exp_field is not None, "experience dropdown not found"
    assert exp_field.options, "options list should not be empty"
    assert any("3-5" in opt for opt in exp_field.options)


async def test_extracts_checkbox(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    checkboxes = [f for f in fields if f.field_type == "checkbox"]
    assert checkboxes, "No checkbox fields found"


async def test_extracts_file_input(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    file_fields = [f for f in fields if f.field_type == "file"]
    assert file_fields, "No file fields found"
    assert any("resume" in f.label.lower() for f in file_fields)


async def test_skips_hidden_inputs(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    # utf8 and authenticity_token are hidden — should be skipped
    assert not any("utf8" in l for l in labels)
    assert not any("authenticity_token" in l or "token" == l for l in labels)


async def test_skips_search_noise_fields(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    assert not any(l == "search" for l in labels)


async def test_detects_eeo_field(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    gender_field = next((f for f in fields if "gender" in f.label.lower()), None)
    assert gender_field is not None, "gender field not found"
    assert is_eeo_field(gender_field.label), "gender should be identified as EEO"


async def test_all_fields_have_selectors(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    assert fields, "No fields extracted"
    for f in fields:
        assert f.selector, f"Field {f.label!r} has no selector"


async def test_required_fields_detected(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    required = [f for f in fields if f.required]
    assert required, "No required fields detected"
    req_labels = [f.label.lower() for f in required]
    assert any("first name" in l for l in req_labels)


async def test_no_duplicate_labels(http_server, page):
    await page.goto(f"{http_server}/scraper_fields.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label for f in fields]
    assert len(labels) == len(set(labels)), f"Duplicate labels found: {labels}"


async def test_greenhouse_application_fields(http_server, page):
    """Full Greenhouse application form — all expected fields present."""
    await page.goto(f"{http_server}/greenhouse_application.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]

    expected = ["first name", "last name", "email"]
    for exp in expected:
        assert any(exp in l for l in labels), f"Expected field '{exp}' not found in {labels}"


async def test_lever_application_fields(http_server, page):
    """Full Lever application form — all expected fields present."""
    await page.goto(f"{http_server}/lever_application.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]

    expected = ["full name", "email"]
    for exp in expected:
        assert any(exp in l for l in labels), f"Expected field '{exp}' not found in {labels}"


async def test_empty_page_returns_empty_list(page):
    await page.set_content("<html><body><p>No form here</p></body></html>")
    fields = await extract_fields_from_page(page)
    assert fields == []
