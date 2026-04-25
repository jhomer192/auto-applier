"""Tests for bot/human.py — timing utilities and stealth context configuration.

Most functions here touch Playwright (which needs a real browser) or sleep for
real durations. We mock asyncio.sleep everywhere and use lightweight fakes for
Playwright objects so the logic can be tested without launching Chromium.
"""
import asyncio
import random
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from bot.human import (
    HUMAN_TYPE_MAX_CHARS,
    _LOCALES,
    _TIMEZONES,
    _USER_AGENTS,
    _VIEWPORTS,
    after_click_pause,
    field_transition_pause,
    human_click,
    human_scroll,
    human_type,
    jitter_pause,
    launch_stealth_context,
    page_load_pause,
    read_pause,
)


# ── Constants ────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_human_type_max_chars_is_positive(self):
        assert HUMAN_TYPE_MAX_CHARS > 0

    def test_viewports_all_have_width_and_height(self):
        for vp in _VIEWPORTS:
            assert "width" in vp and "height" in vp
            assert vp["width"] > 0 and vp["height"] > 0

    def test_user_agents_are_non_empty_strings(self):
        for ua in _USER_AGENTS:
            assert isinstance(ua, str) and len(ua) > 20

    def test_locales_weighted_toward_en_us(self):
        en_us_count = _LOCALES.count("en-US")
        assert en_us_count >= 3, "en-US should be the dominant locale"

    def test_timezones_all_us(self):
        for tz in _TIMEZONES:
            assert tz.startswith("America/")


# ── Timing utilities ─────────────────────────────────────────────────────────────

class TestJitterPause:
    @pytest.mark.asyncio
    async def test_calls_sleep(self):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await jitter_pause(1000)
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_sleep_within_variance_bounds(self):
        """Sleep duration must be in [base*(1-v), base*(1+v)]."""
        sleep_vals = []

        async def capture_sleep(secs):
            sleep_vals.append(secs)

        with patch("asyncio.sleep", side_effect=capture_sleep):
            for _ in range(30):
                await jitter_pause(1000, variance=0.4)

        for s in sleep_vals:
            assert 0.6 <= s <= 1.4 + 0.001, f"Sleep {s}s outside [0.6, 1.4]"

    @pytest.mark.asyncio
    async def test_zero_base_sleeps_zero(self):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await jitter_pause(0)
        secs = mock_sleep.call_args[0][0]
        assert secs == 0.0

    @pytest.mark.asyncio
    async def test_values_are_varied(self):
        """Consecutive calls should NOT produce identical sleep times."""
        sleep_vals = []

        async def capture(secs):
            sleep_vals.append(secs)

        with patch("asyncio.sleep", side_effect=capture):
            for _ in range(10):
                await jitter_pause(1000)

        # Not all values are equal (astronomically unlikely to be the same 10 times)
        assert len(set(sleep_vals)) > 1


class TestPageLoadPause:
    @pytest.mark.asyncio
    async def test_sleeps_between_1_and_3_2(self):
        captured = []

        async def capture(secs):
            captured.append(secs)

        with patch("asyncio.sleep", side_effect=capture):
            for _ in range(20):
                await page_load_pause()

        for s in captured:
            assert 1.0 <= s <= 3.2 + 0.001, f"Sleep {s}s outside [1.0, 3.2]"


class TestAfterClickPause:
    @pytest.mark.asyncio
    async def test_sleeps_between_0_4_and_1_1(self):
        captured = []

        async def capture(secs):
            captured.append(secs)

        with patch("asyncio.sleep", side_effect=capture):
            for _ in range(20):
                await after_click_pause()

        for s in captured:
            assert 0.4 <= s <= 1.1 + 0.001, f"Sleep {s}s outside [0.4, 1.1]"


class TestFieldTransitionPause:
    @pytest.mark.asyncio
    async def test_sleeps_between_0_3_and_0_9(self):
        captured = []

        async def capture(secs):
            captured.append(secs)

        with patch("asyncio.sleep", side_effect=capture):
            for _ in range(20):
                await field_transition_pause()

        for s in captured:
            assert 0.3 <= s <= 0.9 + 0.001, f"Sleep {s}s outside [0.3, 0.9]"


class TestReadPause:
    @pytest.mark.asyncio
    async def test_short_content_sleeps_quickly(self):
        """Very short content (100 chars) should sleep less than long content."""
        short_sleeps = []
        long_sleeps = []

        async def capture_short(s):
            short_sleeps.append(s)

        async def capture_long(s):
            long_sleeps.append(s)

        with patch("asyncio.sleep", side_effect=capture_short):
            for _ in range(10):
                await read_pause(100)

        with patch("asyncio.sleep", side_effect=capture_long):
            for _ in range(10):
                await read_pause(10000)

        assert sum(short_sleeps) / len(short_sleeps) < sum(long_sleeps) / len(long_sleeps)

    @pytest.mark.asyncio
    async def test_very_long_content_capped(self):
        """Even with extremely long content the sleep should be capped."""
        captured = []

        async def capture(s):
            captured.append(s)

        with patch("asyncio.sleep", side_effect=capture):
            for _ in range(10):
                await read_pause(100_000)

        # Base is 0.8–2.0, read contributes at most 6 * 0.6 = 3.6 → max ~5.6
        for s in captured:
            assert s <= 8.0, f"Sleep {s}s too long for capped read_pause"


# ── Typing ───────────────────────────────────────────────────────────────────────

def _make_page_for_typing() -> MagicMock:
    """Build a minimal page mock that supports the ops human_type uses."""
    page = MagicMock()
    page.click = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    page.keyboard.type = AsyncMock()
    return page


class TestHumanType:
    @pytest.mark.asyncio
    async def test_empty_text_does_nothing(self):
        page = _make_page_for_typing()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#field", "")
        page.click.assert_not_called()
        page.keyboard.type.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_text_types_char_by_char(self):
        """Short text (<= HUMAN_TYPE_MAX_CHARS) must call keyboard.type once per char."""
        page = _make_page_for_typing()
        short_text = "Hello"
        assert len(short_text) <= HUMAN_TYPE_MAX_CHARS

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#field", short_text)

        # keyboard.type should be called once per character
        type_calls = page.keyboard.type.call_args_list
        typed_chars = [c[0][0] for c in type_calls]
        assert typed_chars == list(short_text)

    @pytest.mark.asyncio
    async def test_long_text_uses_paste_mode(self):
        """Long text (> HUMAN_TYPE_MAX_CHARS) must call keyboard.type once with delay kwarg."""
        page = _make_page_for_typing()
        long_text = "x" * (HUMAN_TYPE_MAX_CHARS + 1)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#field", long_text)

        # Should be exactly 1 call to keyboard.type with the full text
        type_calls = page.keyboard.type.call_args_list
        assert len(type_calls) == 1
        assert type_calls[0][0][0] == long_text
        # And it should have a delay kwarg
        assert "delay" in type_calls[0][1]

    @pytest.mark.asyncio
    async def test_long_text_delay_in_realistic_range(self):
        """Paste-speed delay should be 18–35ms."""
        page = _make_page_for_typing()
        long_text = "x" * (HUMAN_TYPE_MAX_CHARS + 50)

        delays = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            for _ in range(20):
                page.keyboard.type.reset_mock()
                await human_type(page, "#field", long_text)
                delay = page.keyboard.type.call_args[1]["delay"]
                delays.append(delay)

        for d in delays:
            assert 18 <= d <= 35, f"Paste delay {d}ms outside [18, 35]"

    @pytest.mark.asyncio
    async def test_clears_field_before_typing(self):
        """Must select-all + delete before typing."""
        page = _make_page_for_typing()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#field", "hi")

        press_calls = [c[0][0] for c in page.keyboard.press.call_args_list]
        assert "Control+a" in press_calls
        assert "Delete" in press_calls

    @pytest.mark.asyncio
    async def test_clicks_selector_first(self):
        page = _make_page_for_typing()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#myfield", "test")
        page.click.assert_called_once_with("#myfield")

    @pytest.mark.asyncio
    async def test_threshold_boundary_exact_max_uses_char_by_char(self):
        """Text exactly HUMAN_TYPE_MAX_CHARS should use char-by-char path."""
        page = _make_page_for_typing()
        text = "a" * HUMAN_TYPE_MAX_CHARS

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_type(page, "#f", text)

        type_calls = page.keyboard.type.call_args_list
        # Each call should be a single character
        for c in type_calls:
            assert len(c[0][0]) == 1


# ── Mouse ─────────────────────────────────────────────────────────────────────────

def _make_page_for_click(box=None) -> MagicMock:
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()

    locator = MagicMock()
    locator.bounding_box = AsyncMock(return_value=box)
    locator.click = AsyncMock()
    page.locator = MagicMock(return_value=MagicMock(first=locator))

    return page, locator


class TestHumanClick:
    @pytest.mark.asyncio
    async def test_uses_bounding_box_when_available(self):
        box = {"x": 100, "y": 200, "width": 80, "height": 40}
        page, locator = _make_page_for_click(box)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_click(page, "button#submit")

        page.mouse.move.assert_called_once()
        page.mouse.click.assert_called_once()
        locator.click.assert_not_called()  # Should NOT fall back to locator.click

    @pytest.mark.asyncio
    async def test_click_within_inner_70_percent(self):
        box = {"x": 0, "y": 0, "width": 200, "height": 100}
        page, _ = _make_page_for_click(box)

        xs, ys = [], []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            for _ in range(50):
                page.mouse.move.reset_mock()
                page.mouse.click.reset_mock()
                await human_click(page, "button")
                args = page.mouse.click.call_args[0]
                xs.append(args[0])
                ys.append(args[1])

        for x in xs:
            assert 30 <= x <= 170, f"Click x={x} outside inner 70% (30–170)"
        for y in ys:
            assert 15 <= y <= 85, f"Click y={y} outside inner 70% (15–85)"

    @pytest.mark.asyncio
    async def test_falls_back_to_locator_click_when_no_box(self):
        page, locator = _make_page_for_click(box=None)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_click(page, "button")

        locator.click.assert_called_once()
        page.mouse.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_when_box_has_zero_dimensions(self):
        box = {"x": 100, "y": 100, "width": 0, "height": 0}
        page, locator = _make_page_for_click(box)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_click(page, "button")

        locator.click.assert_called_once()


# ── Scrolling ─────────────────────────────────────────────────────────────────────

def _make_page_for_scroll() -> MagicMock:
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    return page


class TestHumanScroll:
    @pytest.mark.asyncio
    async def test_scrolls_in_multiple_steps(self):
        page = _make_page_for_scroll()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_scroll(page, pixels=300)

        call_count = page.mouse.wheel.call_count
        assert 3 <= call_count <= 6, f"Expected 3–6 scroll steps, got {call_count}"

    @pytest.mark.asyncio
    async def test_total_scroll_approximately_correct(self):
        """Sum of wheel deltas should be near the requested pixel count (±40%)."""
        page = _make_page_for_scroll()
        target = 400
        deltas = []

        async def capture_wheel(x, y):
            deltas.append(y)

        page.mouse.wheel.side_effect = capture_wheel

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await human_scroll(page, pixels=target)

        total = sum(deltas)
        assert target * 0.5 <= total <= target * 1.5, f"Total scroll {total} far from {target}"

    @pytest.mark.asyncio
    async def test_random_pixels_when_none(self):
        """No pixels arg → picks a random value in [150, 500]."""
        page = _make_page_for_scroll()
        all_totals = []

        for _ in range(15):
            deltas = []

            async def capture(x, y):
                deltas.append(y)

            page.mouse.wheel.side_effect = capture
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await human_scroll(page)

            all_totals.append(sum(deltas))

        # Not all identical (random pick each call)
        assert len(set(round(t, -1) for t in all_totals)) > 1, "Scroll totals suspiciously uniform"


# ── Stealth context ───────────────────────────────────────────────────────────────

class TestLaunchStealthContext:
    @pytest.mark.asyncio
    async def test_returns_browser_and_context(self):
        """launch_stealth_context must return (browser, ctx) tuple."""
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        browser, ctx = await launch_stealth_context(mock_playwright)
        assert browser is mock_browser
        assert ctx is mock_ctx

    @pytest.mark.asyncio
    async def test_init_script_is_injected(self):
        """Stealth JS must be injected into every context via add_init_script."""
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        await launch_stealth_context(mock_playwright)
        mock_ctx.add_init_script.assert_called_once()

    @pytest.mark.asyncio
    async def test_stealth_script_disables_webdriver(self):
        """The injected script must reference navigator.webdriver."""
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        await launch_stealth_context(mock_playwright)
        script = mock_ctx.add_init_script.call_args[0][0]
        assert "webdriver" in script

    @pytest.mark.asyncio
    async def test_passes_storage_state_when_provided(self):
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        await launch_stealth_context(mock_playwright, auth_state="/tmp/auth.json")

        ctx_kwargs = mock_browser.new_context.call_args[1]
        assert ctx_kwargs.get("storage_state") == "/tmp/auth.json"

    @pytest.mark.asyncio
    async def test_no_storage_state_when_not_provided(self):
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        await launch_stealth_context(mock_playwright)

        ctx_kwargs = mock_browser.new_context.call_args[1]
        assert "storage_state" not in ctx_kwargs

    @pytest.mark.asyncio
    async def test_automation_flag_disabled(self):
        """AutomationControlled must be in the browser launch args."""
        mock_ctx = MagicMock()
        mock_ctx.add_init_script = AsyncMock()
        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        await launch_stealth_context(mock_playwright)

        launch_kwargs = mock_playwright.chromium.launch.call_args[1]
        args = launch_kwargs.get("args", [])
        assert any("AutomationControlled" in a for a in args)

    @pytest.mark.asyncio
    async def test_fingerprint_is_randomised_across_calls(self):
        """Different calls should yield different user agents."""
        uas = []
        for _ in range(10):
            mock_ctx = MagicMock()
            mock_ctx.add_init_script = AsyncMock()
            mock_browser = MagicMock()
            mock_browser.new_context = AsyncMock(return_value=mock_ctx)

            mock_playwright = MagicMock()
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

            await launch_stealth_context(mock_playwright)
            ctx_kwargs = mock_browser.new_context.call_args[1]
            uas.append(ctx_kwargs.get("user_agent"))

        # With 10 calls and 8 user agents, we should see at least 2 different values
        assert len(set(uas)) >= 2, "User agent never varied across 10 calls"
