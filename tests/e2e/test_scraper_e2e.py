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


# ── Advanced / edge-case extraction (scraper_advanced.html) ───────────────────


async def test_extracts_radio_buttons(http_server, page):
    """Radio inputs must be extracted with field_type='radio'."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    radio_fields = [f for f in fields if f.field_type == "radio"]
    assert radio_fields, "No radio fields extracted"
    labels = [f.label.lower() for f in radio_fields]
    assert any("immediate" in l or "availability" in l or "weeks" in l or "month" in l
               for l in labels), f"Radio labels not recognised: {labels}"


async def test_select_with_optgroup_flattens_options(http_server, page):
    """Options inside <optgroup> must all appear in the field's options list."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    dept = next((f for f in fields if "department" in f.label.lower()), None)
    assert dept is not None, "Department field not found"
    assert dept.field_type == "select"
    # Options from both Engineering and Product optgroups
    assert "Backend" in dept.options or "backend" in [o.lower() for o in dept.options]
    assert "Frontend" in dept.options or "frontend" in [o.lower() for o in dept.options]
    assert "Product Manager" in dept.options or any("product" in o.lower() for o in dept.options)


async def test_select_placeholder_only_returns_empty_options(http_server, page):
    """A select with only an empty-value placeholder should return empty options list."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    empty_sel = next((f for f in fields if "empty select" in f.label.lower()), None)
    assert empty_sel is not None, "Empty select not found"
    assert empty_sel.field_type == "select"
    # The only option has value="" and is filtered out by the scraper
    assert empty_sel.options == [], f"Expected no options, got: {empty_sel.options}"


async def test_skips_display_none_fields(http_server, page):
    """Fields with display:none must not be returned."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    assert not any("invisible" in l for l in labels), (
        f"display:none field was extracted: {labels}"
    )


async def test_aria_required_textarea_marked_required(http_server, page):
    """Textarea with aria-required='true' must have required=True."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    cover = next((f for f in fields if "cover letter" in f.label.lower()), None)
    assert cover is not None, "Cover Letter textarea not found"
    assert cover.field_type == "textarea"
    assert cover.required is True, "aria-required='true' must set required=True"


async def test_aria_labelledby_resolution(http_server, page):
    """Textarea with aria-labelledby must resolve the label from the referenced element."""
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    # The cover letter textarea uses aria-labelledby="cover-letter-label"
    cover = next((f for f in fields if "cover letter" in f.label.lower()), None)
    assert cover is not None, "aria-labelledby label not resolved"


async def test_readonly_field_is_extracted(http_server, page):
    """Readonly inputs are visible and rendered — the scraper must include them.
    Adapters are responsible for deciding not to fill readonly fields.
    """
    await page.goto(f"{http_server}/scraper_advanced.html")
    fields = await extract_fields_from_page(page)
    labels = [f.label.lower() for f in fields]
    assert any("company name" in l or "pre-filled" in l for l in labels), (
        f"Readonly field not extracted, labels: {labels}"
    )


# ── field_answer_hint edge cases ──────────────────────────────────────────────


async def test_field_answer_hint_radio_returns_none():
    """Radio buttons have no hint — LLM should use its own judgment."""
    from bot.scraper import field_answer_hint
    from bot.models import FormField
    field = FormField(
        label="Availability", field_type="radio", required=False, selector="#avail"
    )
    # Radio is not explicitly handled — should return None (no special guidance)
    hint = field_answer_hint(field)
    assert hint is None


async def test_field_answer_hint_select_with_many_options_caps_at_20():
    """Select with >20 options: hint must only list the first 20."""
    from bot.scraper import field_answer_hint
    from bot.models import FormField
    many_opts = [f"Option {i}" for i in range(30)]
    field = FormField(
        label="Big List", field_type="select", required=False, selector="#big",
        options=many_opts,
    )
    hint = field_answer_hint(field)
    assert hint is not None
    listed = [o for o in many_opts if o in hint]
    assert len(listed) <= 20, f"Hint listed more than 20 options: {len(listed)}"


async def test_field_answer_hint_salary_field():
    from bot.scraper import field_answer_hint
    from bot.models import FormField
    field = FormField(label="Expected Salary", field_type="text", required=False, selector="#s")
    hint = field_answer_hint(field)
    assert hint is not None
    assert "NEEDS_USER_INPUT" in hint


async def test_field_answer_hint_visa_field():
    from bot.scraper import field_answer_hint
    from bot.models import FormField
    field = FormField(label="Legally eligible to work in the US?", field_type="select",
                      required=True, selector="#v", options=["Yes", "No", "Need sponsorship"])
    hint = field_answer_hint(field)
    assert hint is not None
    assert len(hint) > 0
