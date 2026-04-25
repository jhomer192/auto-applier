"""Human-like Playwright interaction utilities.

All adapters use this module for timing, mouse movement, typing, and browser
fingerprinting. The goal is to be indistinguishable from a real Chrome user
on a mid-spec laptop.

Design principles:
- Every delay is randomised within a realistic human range.
- Typing fires proper keyboard events (bypasses React form detection).
- Short fields (<= HUMAN_TYPE_MAX_CHARS) get character-by-character typing.
- Long fields (cover letters etc.) get a faster "paste-like" type that still
  fires events but at ~25ms/char so it doesn't take minutes.
- Mouse clicks include a randomised landing point within the element bounding box.
- Browser context looks like a real Chrome installation.
"""
import asyncio
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Fields longer than this are typed at "paste speed" rather than "keystroke speed"
HUMAN_TYPE_MAX_CHARS = 120

# ── Browser fingerprinting ──────────────────────────────────────────────────────

_VIEWPORTS = [
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
]

_USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

_LOCALES = ["en-US", "en-US", "en-US", "en-GB", "en-CA"]  # weighted toward en-US

_TIMEZONES = [
    "America/New_York", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Los_Angeles",
    "America/Phoenix", "America/New_York",
]

# Injected into every page to suppress webdriver detection signals
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
window.chrome = { runtime: {} };
delete window.__playwright;
delete window.__pwInitScripts;
"""


async def launch_stealth_context(playwright, auth_state: Optional[str] = None):
    """Launch a Chromium browser with a randomised, human-like fingerprint.

    Returns (browser, context). Caller is responsible for closing both.

    Args:
        playwright: The Playwright instance (from async_playwright().__aenter__).
        auth_state: Optional path to saved storage state (e.g. LinkedIn cookies).
    """
    viewport = random.choice(_VIEWPORTS)
    ua = random.choice(_USER_AGENTS)
    locale = random.choice(_LOCALES)
    timezone_id = random.choice(_TIMEZONES)

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
    )

    ctx_kwargs: dict = dict(
        viewport=viewport,
        user_agent=ua,
        locale=locale,
        timezone_id=timezone_id,
        color_scheme="light",
        java_script_enabled=True,
        accept_downloads=True,
        extra_http_headers={
            "Accept-Language": f"{locale},en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    if auth_state:
        ctx_kwargs["storage_state"] = auth_state

    ctx = await browser.new_context(**ctx_kwargs)
    await ctx.add_init_script(_STEALTH_SCRIPT)

    return browser, ctx


# ── Timing utilities ────────────────────────────────────────────────────────────

async def jitter_pause(base_ms: int, variance: float = 0.4) -> None:
    """Sleep for base_ms ± (base_ms * variance), uniformly distributed.

    Example: jitter_pause(1000) sleeps 600–1400ms.
    """
    lo = base_ms * (1.0 - variance)
    hi = base_ms * (1.0 + variance)
    await asyncio.sleep(random.uniform(lo, hi) / 1000.0)


async def page_load_pause() -> None:
    """Pause after navigation, simulating human reaction to page load (1.0–3.2s)."""
    await asyncio.sleep(random.uniform(1.0, 3.2))


async def after_click_pause() -> None:
    """Brief pause after clicking a button (0.4–1.1s)."""
    await asyncio.sleep(random.uniform(0.4, 1.1))


async def field_transition_pause() -> None:
    """Pause between filling one field and the next (0.3–0.9s)."""
    await asyncio.sleep(random.uniform(0.3, 0.9))


async def read_pause(content_length: int = 400) -> None:
    """Simulate reading the page content.

    Scales with content length (chars), capped at 6 seconds.
    Adds a random base "glance" of 0.8–2.0s.
    """
    chars_per_sec = 1200  # ~200 wpm * 6 chars/word
    read_secs = min(content_length / chars_per_sec, 6.0)
    base = random.uniform(0.8, 2.0)
    await asyncio.sleep(base + read_secs * random.uniform(0.25, 0.6))


# ── Typing ──────────────────────────────────────────────────────────────────────

async def human_type(page, selector: str, text: str) -> None:
    """Type text into a field with human-like keystroke timing.

    Short text (<= HUMAN_TYPE_MAX_CHARS): full variable-speed char-by-char.
    Long text (> HUMAN_TYPE_MAX_CHARS): faster "paste-like" mode (~25ms/char)
      that still fires keyboard events (unlike page.fill which sets value directly).

    Always clears the field first with triple-click.
    """
    # Click to focus
    await page.click(selector)
    await asyncio.sleep(random.uniform(0.12, 0.35))

    # Select all existing content and clear it
    await page.keyboard.press("Control+a")
    await asyncio.sleep(random.uniform(0.05, 0.12))
    await page.keyboard.press("Delete")
    await asyncio.sleep(random.uniform(0.05, 0.15))

    if not text:
        return  # empty string: clear only, no typing

    if len(text) <= HUMAN_TYPE_MAX_CHARS:
        # Full human-speed typing
        chars_since_pause = 0
        next_pause_at = random.randint(6, 14)

        for char in text:
            await page.keyboard.type(char)
            chars_since_pause += 1

            # Base keystroke delay: 45–125ms
            delay = random.uniform(0.045, 0.125)

            # Periodic "thinking" pause every 6–14 chars
            if chars_since_pause >= next_pause_at:
                delay += random.uniform(0.12, 0.45)
                chars_since_pause = 0
                next_pause_at = random.randint(6, 14)

            await asyncio.sleep(delay)
    else:
        # Paste-speed typing: ~25ms/char, still fires keyboard events
        await page.keyboard.type(text, delay=random.randint(18, 35))

    # Brief pause after completing the field
    await asyncio.sleep(random.uniform(0.15, 0.4))


# ── Mouse ───────────────────────────────────────────────────────────────────────

async def human_click(page, selector: str) -> None:
    """Move the mouse to the element and click at a randomised point within it.

    Gets the element's bounding box and clicks within the inner 70% of its area
    (avoids the very edges where real users rarely click). Falls back to a plain
    locator click if the bounding box can't be retrieved.
    """
    locator = page.locator(selector).first
    box = None
    try:
        box = await locator.bounding_box()
    except Exception:
        pass

    if box and box["width"] > 0 and box["height"] > 0:
        # Random point in inner 70% of element
        pad_x = box["width"] * 0.15
        pad_y = box["height"] * 0.15
        x = box["x"] + pad_x + random.uniform(0, box["width"] - 2 * pad_x)
        y = box["y"] + pad_y + random.uniform(0, box["height"] - 2 * pad_y)

        # Brief hover before clicking (40–180ms)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.04, 0.18))
        await page.mouse.click(x, y)
    else:
        await locator.click()

    await asyncio.sleep(random.uniform(0.08, 0.22))


async def human_scroll(page, pixels: Optional[int] = None) -> None:
    """Scroll the page gradually in small steps, simulating reading.

    If pixels is None, picks a random distance between 150 and 500px.
    """
    if pixels is None:
        pixels = random.randint(150, 500)

    steps = random.randint(3, 6)
    per_step = pixels / steps
    for _ in range(steps):
        delta = per_step + random.uniform(-15, 15)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.06, 0.2))
