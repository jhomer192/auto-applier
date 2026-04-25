"""E2E tests for bot/human.py — real browser interactions.

Verifies that human_type fills inputs, human_click fires events,
human_scroll doesn't crash, and launch_stealth_context returns a
usable browser with stealth settings applied.
"""
import pytest
from bot.human import (
    human_click,
    human_scroll,
    human_type,
    launch_stealth_context,
)
from playwright.async_api import async_playwright

pytestmark = pytest.mark.asyncio


# ── human_type ────────────────────────────────────────────────────────────────

async def test_human_type_fills_text_input(http_server, page):
    await page.goto(f"{http_server}/human_interaction.html")
    await human_type(page, "#text_input", "Hello World")
    value = await page.input_value("#text_input")
    assert value == "Hello World"


async def test_human_type_fills_textarea(http_server, page):
    await page.goto(f"{http_server}/human_interaction.html")
    text = "This is a longer cover letter text that tests pasting behavior for long inputs."
    await human_type(page, "#long_textarea", text)
    value = await page.input_value("#long_textarea")
    assert value == text


async def test_human_type_clears_existing_value(http_server, page):
    await page.goto(f"{http_server}/human_interaction.html")
    await page.fill("#text_input", "old value")
    await human_type(page, "#text_input", "new value")
    value = await page.input_value("#text_input")
    assert value == "new value"


async def test_human_type_empty_string_is_noop(http_server, page):
    """Empty string must not interact with the field — adapters skip no-answer fields."""
    await page.goto(f"{http_server}/human_interaction.html")
    await page.fill("#text_input", "something")
    await human_type(page, "#text_input", "")
    value = await page.input_value("#text_input")
    # Field should be untouched — no click, no clear
    assert value == "something"


async def test_human_type_long_text_uses_fill(http_server, page):
    """Text > 120 chars should use paste-speed (fill), still produces correct result."""
    await page.goto(f"{http_server}/human_interaction.html")
    long_text = "A" * 150  # exceeds the 120-char threshold
    await human_type(page, "#long_textarea", long_text)
    value = await page.input_value("#long_textarea")
    assert value == long_text


# ── human_click ───────────────────────────────────────────────────────────────

async def test_human_click_fires_click_event(http_server, page):
    await page.goto(f"{http_server}/human_interaction.html")
    # click_result is hidden before click
    initial = await page.is_visible("#click_result")
    assert not initial
    await human_click(page, "#click_target")
    visible = await page.is_visible("#click_result")
    assert visible, "click event was not fired"


async def test_human_click_button_works(http_server, page):
    await page.goto(f"{http_server}/human_interaction.html")
    await human_click(page, "#submit_btn")
    visible = await page.is_visible("#click_result")
    assert visible


# ── human_scroll ──────────────────────────────────────────────────────────────

async def test_human_scroll_does_not_crash(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    # Should complete without raising
    await human_scroll(page)


async def test_human_scroll_with_pixels(http_server, page):
    await page.goto(f"{http_server}/greenhouse_application.html")
    await human_scroll(page, pixels=500)


# ── launch_stealth_context ────────────────────────────────────────────────────

async def test_launch_stealth_context_returns_usable_browser():
    """launch_stealth_context should return a (browser, context) tuple and
    the resulting page should be able to navigate and evaluate JS."""
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p)
        try:
            page = await ctx.new_page()
            await page.set_content("<html><body><div id='test'>ok</div></body></html>")
            text = await page.text_content("#test")
            assert text == "ok"
        finally:
            await browser.close()


async def test_launch_stealth_context_hides_webdriver():
    """The stealth script should mask navigator.webdriver."""
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p)
        try:
            page = await ctx.new_page()
            await page.set_content("<html><body></body></html>")
            webdriver = await page.evaluate("() => navigator.webdriver")
            # Stealth script should make this falsy
            assert not webdriver, "navigator.webdriver should be masked"
        finally:
            await browser.close()


async def test_launch_stealth_context_user_agent_is_realistic():
    """User agent should look like a real browser, not Playwright/HeadlessChrome."""
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p)
        try:
            page = await ctx.new_page()
            await page.set_content("<html></html>")
            ua = await page.evaluate("() => navigator.userAgent")
            assert "Mozilla" in ua
            assert "HeadlessChrome" not in ua, f"User agent reveals headless: {ua}"
        finally:
            await browser.close()
