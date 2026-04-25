"""Shared fixtures for Playwright E2E tests.

Key design decisions:
  - A ThreadingHTTPServer serves `tests/e2e/pages/` so adapters get real HTTP
    responses rather than file:// URIs.
  - All timing helpers are monkey-patched to return instantly so tests
    complete in seconds, not minutes.
  - The `page` fixture is a pure-async, function-scoped fixture that launches
    a fresh Chromium browser per test. pytest-asyncio 1.x module-scoped async
    fixtures require loop_scope alignment that is brittle across versions;
    function scope is simpler and reliable (~6s/test on this VPS).
  - `screenshot_on_failure` only activates for tests that actually use the
    `page` fixture (no phantom browser launches for pure unit tests).
"""
import asyncio
import http.server
import threading
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

PAGES_DIR = Path(__file__).parent / "pages"
SCREENSHOTS_DIR = Path(__file__).parent.parent.parent / "data" / "screenshots"


# ── HTTP server ───────────────────────────────────────────────────────────────

class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGES_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture(scope="session")
def http_server():
    """Session-scoped local HTTP server. Returns base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SilentHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ── Fake resume ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def resume_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("resume") / "resume.pdf"
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n"
        b"0000000058 00000 n\n0000000115 00000 n\n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
    )
    return str(path)


# ── Instant timing ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def instant_pauses(monkeypatch):
    """Replace all human-like pauses with near-instant coroutines."""
    async def _noop(*args, **kwargs):
        await asyncio.sleep(0)

    import bot.human as _human
    monkeypatch.setattr(_human, "jitter_pause", _noop)
    monkeypatch.setattr(_human, "page_load_pause", _noop)
    monkeypatch.setattr(_human, "after_click_pause", _noop)
    monkeypatch.setattr(_human, "field_transition_pause", _noop)
    monkeypatch.setattr(_human, "read_pause", _noop)


# ── Page fixture (function-scoped, async) ────────────────────────────────────

@pytest_asyncio.fixture
async def page():
    """Fresh Chromium browser + context + page per test.

    One browser launch per test (~5-6s on this VPS) is the safest option for
    pytest-asyncio 1.x in strict mode: module-scoped async fixtures require
    matching loop_scope, which varies across minor versions.
    """
    async with async_playwright() as p:
        br = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await br.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        pg = await ctx.new_page()
        yield pg
        await br.close()


# ── Screenshot on failure (only for tests that use `page`) ───────────────────

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest_asyncio.fixture(autouse=True)
async def screenshot_on_failure(request):
    """Capture a screenshot on failure — only when the test uses the page fixture."""
    yield
    # Only act if this test actually requested the page fixture
    if "page" not in request.fixturenames:
        return
    report = getattr(request.node, "rep_call", None)
    if not (report and report.failed):
        return
    try:
        pg = request.getfixturevalue("page")
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        safe = request.node.name.replace("/", "_").replace(":", "_")
        await pg.screenshot(path=str(SCREENSHOTS_DIR / f"e2e_fail_{safe}.png"))
    except Exception:
        pass
