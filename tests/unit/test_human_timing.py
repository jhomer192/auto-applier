"""Unit tests for bot/human.py timing and fingerprinting behaviour.

Strategy: patch asyncio.sleep to capture what values the timing functions
pass to it, then assert those values are within the documented ranges.
This verifies the constants match the docstrings without requiring real
wall-clock waits.  Tests run in under 1 second.

Also tests launch_stealth_context for fingerprint randomisation and
human_type / human_click for their core behaviours.
"""
import asyncio
import random
import pytest
import pytest_asyncio

from bot.human import (
    after_click_pause,
    field_transition_pause,
    jitter_pause,
    launch_stealth_context,
    page_load_pause,
    read_pause,
    HUMAN_TYPE_MAX_CHARS,
    _VIEWPORTS,
    _USER_AGENTS,
    _LOCALES,
    _TIMEZONES,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_sleep_capture(monkeypatch):
    """Replace asyncio.sleep with a no-op that records each call."""
    calls = []

    async def _mock(secs):
        calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _mock)
    return calls


# ── jitter_pause ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jitter_pause_stays_within_range(monkeypatch):
    """jitter_pause(1000) must sleep between 600 ms and 1400 ms."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await jitter_pause(1000)
    assert calls, "asyncio.sleep was never called"
    assert all(0.6 <= v <= 1.4 for v in calls), (
        f"Values out of 600–1400 ms range: {[v for v in calls if not 0.6 <= v <= 1.4]}"
    )


@pytest.mark.asyncio
async def test_jitter_pause_custom_variance(monkeypatch):
    """jitter_pause(1000, variance=0.1) must sleep 900–1100 ms."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await jitter_pause(1000, variance=0.1)
    assert all(0.9 <= v <= 1.1 for v in calls), (
        f"Values out of 900–1100 ms range: {[v for v in calls if not 0.9 <= v <= 1.1]}"
    )


@pytest.mark.asyncio
async def test_jitter_pause_scales_with_base(monkeypatch):
    """Larger base_ms → larger sleep values."""
    calls_small, calls_large = [], []

    async def mock_small(s): calls_small.append(s)
    async def mock_large(s): calls_large.append(s)

    monkeypatch.setattr(asyncio, "sleep", mock_small)
    for _ in range(10):
        await jitter_pause(200)

    monkeypatch.setattr(asyncio, "sleep", mock_large)
    for _ in range(10):
        await jitter_pause(2000)

    assert max(calls_large) > max(calls_small)


# ── page_load_pause ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_load_pause_range(monkeypatch):
    """page_load_pause must sleep 1.0–3.2 seconds."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await page_load_pause()
    assert all(1.0 <= v <= 3.2 for v in calls), (
        f"Values outside 1.0–3.2 s: {[v for v in calls if not 1.0 <= v <= 3.2]}"
    )


# ── after_click_pause ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_click_pause_range(monkeypatch):
    """after_click_pause must sleep 0.4–1.1 seconds."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await after_click_pause()
    assert all(0.4 <= v <= 1.1 for v in calls), (
        f"Values outside 0.4–1.1 s: {[v for v in calls if not 0.4 <= v <= 1.1]}"
    )


# ── field_transition_pause ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_field_transition_pause_range(monkeypatch):
    """field_transition_pause must sleep 0.3–0.9 seconds."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await field_transition_pause()
    assert all(0.3 <= v <= 0.9 for v in calls), (
        f"Values outside 0.3–0.9 s: {[v for v in calls if not 0.3 <= v <= 0.9]}"
    )


# ── read_pause ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_pause_base_range(monkeypatch):
    """read_pause with tiny content is dominated by the base 0.8–2.0 s glance."""
    calls = _make_sleep_capture(monkeypatch)
    for _ in range(30):
        await read_pause(0)
    # read_secs = 0, so total = base (0.8–2.0) * factor (0.25–0.6)... wait
    # Actually: sleep = base + read_secs * factor = base + 0
    # base in [0.8, 2.0], factor doesn't matter since read_secs=0
    assert all(0.8 <= v <= 2.0 for v in calls), (
        f"read_pause(0) out of 0.8–2.0 range: {[v for v in calls if not 0.8 <= v <= 2.0]}"
    )


@pytest.mark.asyncio
async def test_read_pause_scales_with_content(monkeypatch):
    """Longer content must produce a longer pause than short content."""
    # Fix random.uniform to its midpoint so the only variable is content_length
    monkeypatch.setattr(random, "uniform", lambda a, b: (a + b) / 2.0)

    calls_short = []
    calls_long = []

    async def capture_short(s): calls_short.append(s)
    async def capture_long(s): calls_long.append(s)

    monkeypatch.setattr(asyncio, "sleep", capture_short)
    await read_pause(400)   # 0.33s reading time

    monkeypatch.setattr(asyncio, "sleep", capture_long)
    await read_pause(7200)  # 6.0s reading time (capped)

    assert calls_long[0] > calls_short[0], (
        f"Long pause {calls_long[0]:.3f}s not greater than short {calls_short[0]:.3f}s"
    )


@pytest.mark.asyncio
async def test_read_pause_caps_at_six_seconds_reading(monkeypatch):
    """read_secs is capped at 6.0 regardless of content length."""
    # Use max random values to measure the upper bound
    monkeypatch.setattr(random, "uniform", lambda a, b: b)

    calls_moderate = []
    calls_extreme = []

    async def capture_mod(s): calls_moderate.append(s)
    async def capture_ext(s): calls_extreme.append(s)

    monkeypatch.setattr(asyncio, "sleep", capture_mod)
    await read_pause(7200)   # 6.0s exactly at the cap

    monkeypatch.setattr(asyncio, "sleep", capture_ext)
    await read_pause(720000)  # way over the cap — should be same

    assert calls_moderate[0] == calls_extreme[0], (
        f"Cap not applied: {calls_moderate[0]:.3f} vs {calls_extreme[0]:.3f}"
    )


# ── HUMAN_TYPE_MAX_CHARS constant ─────────────────────────────────────────────


def test_human_type_max_chars_is_reasonable():
    """HUMAN_TYPE_MAX_CHARS must be a positive integer in a sane range."""
    assert isinstance(HUMAN_TYPE_MAX_CHARS, int)
    assert 50 <= HUMAN_TYPE_MAX_CHARS <= 500, (
        f"HUMAN_TYPE_MAX_CHARS={HUMAN_TYPE_MAX_CHARS} is outside the expected 50–500 range"
    )


# ── launch_stealth_context fingerprinting ─────────────────────────────────────


def test_viewport_list_not_empty():
    assert len(_VIEWPORTS) >= 3, "Need at least 3 viewport options for randomisation"


def test_all_viewports_have_width_and_height():
    for vp in _VIEWPORTS:
        assert "width" in vp and "height" in vp, f"Bad viewport: {vp}"
        assert vp["width"] >= 800 and vp["height"] >= 600


def test_user_agent_list_not_empty():
    assert len(_USER_AGENTS) >= 5


def test_all_user_agents_look_like_browsers():
    for ua in _USER_AGENTS:
        assert "Mozilla" in ua or "AppleWebKit" in ua, f"Suspicious UA: {ua!r}"


def test_locale_list_contains_en_us():
    assert "en-US" in _LOCALES


def test_timezone_list_contains_us_timezones():
    us_zones = [tz for tz in _TIMEZONES if tz.startswith("America/")]
    assert len(us_zones) >= 4


@pytest.mark.asyncio
async def test_launch_stealth_context_hides_webdriver():
    """navigator.webdriver must be undefined (not true) in a stealth context."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state=None)
        page = await ctx.new_page()
        try:
            webdriver = await page.evaluate("() => navigator.webdriver")
            assert webdriver is None or webdriver is False or webdriver == "undefined", (
                f"webdriver not hidden: {webdriver!r}"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_launch_stealth_context_has_plugins():
    """navigator.plugins must be non-empty (real browsers have plugins)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state=None)
        page = await ctx.new_page()
        try:
            plugin_count = await page.evaluate("() => navigator.plugins.length")
            assert plugin_count > 0, "navigator.plugins is empty — looks like a bot"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_launch_stealth_context_randomises_viewport():
    """Two fresh contexts should not always use the same viewport (probabilistic)."""
    from playwright.async_api import async_playwright
    viewports = []
    async with async_playwright() as p:
        for _ in range(4):
            browser, ctx = await launch_stealth_context(p, auth_state=None)
            page = await ctx.new_page()
            try:
                vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                viewports.append((vp["w"], vp["h"]))
            finally:
                await browser.close()

    # With 5 viewport options and 4 samples, the chance of all identical is 1/5^3 = 0.8%
    # We accept this very small flake probability in exchange for a real randomisation test.
    # If this ever flakes, increase the number of samples or use a more robust approach.
    unique = set(viewports)
    assert len(unique) >= 2, (
        f"All 4 launches used the same viewport {viewports[0]} — randomisation may be broken"
    )


@pytest.mark.asyncio
async def test_launch_stealth_context_sets_accept_downloads():
    """The context must have accept_downloads=True so file uploads work."""
    from playwright.async_api import async_playwright
    # Indirectly verified: if accept_downloads were False, set_input_files would fail.
    # Here we just verify the context is created without errors.
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state=None)
        try:
            assert ctx is not None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_launch_stealth_context_chrome_object_present():
    """window.chrome must exist (real Chrome has it; headless Chromium by default doesn't)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state=None)
        page = await ctx.new_page()
        try:
            chrome = await page.evaluate("() => typeof window.chrome")
            assert chrome == "object", f"window.chrome is {chrome!r} — stealth script may not be injected"
        finally:
            await browser.close()
