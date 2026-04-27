"""E2E tests for adapter static-fallback selectors and FillFailed guard (audit fix #4).

The static fallback in each adapter ships hard-coded CSS selectors. These selectors
have to match real-world ATS HTML even when the layout has drifted. This test loads
the existing HTML fixtures (which mirror real Greenhouse / Lever layouts), runs each
adapter's ``_static_fallback()``, and asserts each selector matches an input that
can be filled.

It also covers the new ``assert_field_filled`` helper: a stale selector that points
at no input — or an input that was never filled — must raise ``FillFailed`` so we
never submit a blank application.
"""
import pytest

from bot.adapters.base import FillFailed, assert_field_filled
from bot.adapters.greenhouse import _static_fallback as gh_fallback
from bot.adapters.lever import _static_fallback as lv_fallback
from bot.adapters.linkedin import _static_fallback as li_fallback
from bot.models import FormField

pytestmark = pytest.mark.asyncio


# ── Greenhouse static fallback ────────────────────────────────────────────────


async def test_greenhouse_static_fallback_selectors_match_real_html(
    http_server, page, resume_pdf
):
    """Each fallback selector must match an input on a realistic Greenhouse page."""
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)

    fields = gh_fallback()
    answers = {
        "First Name": "Jack",
        "Last Name": "Homer",
        "Email": "jack@example.com",
        "Phone": "555-0123",
        "Resume": resume_pdf,
        "Cover Letter": "I'd be a great fit.",
        "LinkedIn Profile": "https://linkedin.com/in/jhomer",
    }

    # Every field's selector must locate at least one element.
    for f in fields:
        count = await page.locator(f.selector).count()
        assert count > 0, f"Greenhouse fallback selector for {f.label!r} matched 0 elements: {f.selector!r}"

    # Filling each field through its fallback selector must take.
    for f in fields:
        f.answer = answers.get(f.label, "")
        if not f.answer:
            continue
        if f.field_type == "file":
            await page.set_input_files(f.selector, f.answer)
        else:
            await page.fill(f.selector, f.answer)
        if f.field_type != "file":
            await assert_field_filled(page, f)  # must not raise


# ── Lever static fallback ─────────────────────────────────────────────────────


async def test_lever_static_fallback_selectors_match_real_html(
    http_server, page, resume_pdf
):
    url = f"{http_server}/lever_application.html"
    await page.goto(url)

    fields = lv_fallback()
    answers = {
        "Full Name": "Jack Homer",
        "Email": "jack@example.com",
        "Phone": "555-0123",
        "Current Company": "C3 AI",
        "LinkedIn": "https://linkedin.com/in/jhomer",
        "Resume": resume_pdf,
        "Cover Letter": "I'd be a great fit.",
    }

    for f in fields:
        count = await page.locator(f.selector).count()
        assert count > 0, f"Lever fallback selector for {f.label!r} matched 0 elements: {f.selector!r}"

    for f in fields:
        f.answer = answers.get(f.label, "")
        if not f.answer:
            continue
        if f.field_type == "file":
            await page.set_input_files(f.selector, f.answer)
        else:
            await page.fill(f.selector, f.answer)
        if f.field_type != "file":
            await assert_field_filled(page, f)


# ── LinkedIn static fallback (smoke — selectors are syntactically valid) ──────


async def test_linkedin_static_fallback_selectors_are_valid_css(http_server, page):
    """The LinkedIn modal isn't part of our HTML fixtures, so we only verify
    the comma-separated selectors are valid CSS the engine can parse."""
    url = f"{http_server}/greenhouse_application.html"  # any served page
    await page.goto(url)

    fields = li_fallback()
    assert len(fields) >= 3
    for f in fields:
        # locator() doesn't raise on a valid selector that matches nothing.
        # It WILL raise SyntaxError-equivalent on an invalid CSS selector.
        await page.locator(f.selector).count()


# ── FillFailed: positive path — fill present, no raise ───────────────────────


async def test_assert_field_filled_passes_when_input_has_value(http_server, page):
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)
    await page.fill("#email", "jack@example.com")

    field = FormField(
        label="Email", field_type="text", required=True,
        selector="#email", answer="jack@example.com",
    )
    await assert_field_filled(page, field)  # must not raise


# ── FillFailed: stale selector matches no input ──────────────────────────────


async def test_assert_field_filled_raises_when_selector_matches_nothing(
    http_server, page,
):
    """A stale selector on a drifted form — the very condition this guard exists for."""
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)

    field = FormField(
        label="Phone", field_type="text", required=True,
        selector="#nonexistent_input_id", answer="555-1212",
    )
    with pytest.raises(FillFailed, match="matched no input"):
        await assert_field_filled(page, field)


# ── FillFailed: input present but empty (fill silently no-opped) ─────────────


async def test_assert_field_filled_raises_when_value_is_empty(http_server, page):
    """Selector matched, but the value is blank — e.g. the typed input was a
    hidden mirror that didn't accept keystrokes, or the page reset the field."""
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)
    # Note: deliberately do NOT fill #phone

    field = FormField(
        label="Phone", field_type="text", required=True,
        selector="#phone", answer="555-1212",
    )
    with pytest.raises(FillFailed, match="still empty"):
        await assert_field_filled(page, field)


# ── FillFailed: skip rules ───────────────────────────────────────────────────


async def test_assert_field_filled_skips_checkboxes(http_server, page):
    """Checkboxes don't have a meaningful input_value — the helper must skip them."""
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)

    field = FormField(
        label="US Authorized", field_type="checkbox", required=False,
        selector="#us_authorized", answer="yes",
    )
    # Box is unchecked — would be 'empty' if we naively asked for its value, but
    # the helper must skip it cleanly.
    await assert_field_filled(page, field)


async def test_assert_field_filled_skips_empty_answer_fields(http_server, page):
    """Fields we never tried to fill (no answer) must not trip the guard."""
    url = f"{http_server}/greenhouse_application.html"
    await page.goto(url)

    field = FormField(
        label="Phone", field_type="text", required=False,
        selector="#phone", answer="",  # never set
    )
    await assert_field_filled(page, field)
