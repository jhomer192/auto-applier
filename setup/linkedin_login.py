#!/usr/bin/env python3
"""
Headless LinkedIn login on the VPS.

You provide credentials once via env vars; we drive the rest:
  - Submit the login form with stealth fingerprinting.
  - If LinkedIn challenges with 2FA, we screenshot the page, push it to
    your Telegram bot, then wait for you to send a message in the form
    "code 123456" to the bot. The bot writes data/linkedin_2fa_code.txt
    and we pick it up here, fill the field, and continue.
  - On success, save storage state to data/linkedin_auth.json.

Run from /root/auto-applier (tmux recommended so it survives ssh hiccup):

    LINKEDIN_EMAIL='you@example.com' \\
    LINKEDIN_PASSWORD='your-password' \\
    .venv/bin/python setup/linkedin_login.py

The bot must be running (systemctl is-active auto-applier) so the
"code 123456" message routes to a file. If you'd rather not run as
root and don't want creds in shell history, prefix the command with
a single space — most shells skip space-prefixed commands in history.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp


AUTH_STATE_PATH = "data/linkedin_auth.json"
SCREENSHOT_PATH = "data/screenshots/linkedin_login_2fa.png"
CODE_FILE_PATH = "data/linkedin_2fa_code.txt"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_MARKER = "linkedin.com/feed"
LOGIN_TIMEOUT_SECONDS = 60
CODE_WAIT_SECONDS = 600  # 10 min for user to send the code


def _env_or_die(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"ERROR: {key} env var not set.")
        print("Re-run with:")
        print("  LINKEDIN_EMAIL='you@example.com' LINKEDIN_PASSWORD='...' .venv/bin/python setup/linkedin_login.py")
        sys.exit(1)
    return val


async def _send_telegram_photo(caption: str, photo_path: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("(no Telegram creds in env — skipping push; you'll need to view the screenshot file directly)")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        form.add_field("caption", caption)
        form.add_field("photo", open(photo_path, "rb"), filename=Path(photo_path).name)
        async with session.post(url, data=form, timeout=20) as r:
            if r.status != 200:
                body = await r.text()
                print(f"(telegram photo push status={r.status}: {body[:200]})")


async def _wait_for_code() -> str:
    """Block until data/linkedin_2fa_code.txt exists, then return its trimmed contents."""
    Path(CODE_FILE_PATH).unlink(missing_ok=True)
    deadline = time.time() + CODE_WAIT_SECONDS
    while time.time() < deadline:
        if Path(CODE_FILE_PATH).exists():
            try:
                code = Path(CODE_FILE_PATH).read_text().strip()
                Path(CODE_FILE_PATH).unlink(missing_ok=True)
                if code:
                    return code
            except Exception:
                pass
        await asyncio.sleep(1.5)
    raise TimeoutError("No 2FA code received within 10 min. Re-run the script.")


async def _handle_2fa_if_present(page) -> bool:
    """If a 2FA-style code field is visible, screenshot, ping Telegram, fill code.
    Returns True if a 2FA challenge was handled, False if not present.
    """
    selectors = [
        "input#input__phone_verification_pin",
        "input[name='pin']",
        "input[autocomplete='one-time-code']",
        "input[id*='verification']",
        "input[id*='challenge']",
        "input[name='__challengeId']",
    ]
    code_input = None
    for sel in selectors:
        try:
            code_input = await page.wait_for_selector(sel, timeout=2000, state="visible")
            if code_input:
                print(f"2FA challenge detected (selector: {sel})")
                break
        except Exception:
            continue
    if not code_input:
        return False

    Path(SCREENSHOT_PATH).parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=SCREENSHOT_PATH, full_page=False)
    await _send_telegram_photo(
        "LinkedIn is asking for a 2FA code. Reply here with: code <6-digit-code>",
        SCREENSHOT_PATH,
    )
    print("Sent 2FA screenshot to Telegram. Reply with: code 123456")
    code = await _wait_for_code()
    print(f"Got code (length {len(code)}). Filling.")
    await code_input.fill(code)

    submit = None
    for sel in [
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Verify')",
        "button#two-step-submit-button",
    ]:
        try:
            submit = await page.query_selector(sel)
            if submit:
                break
        except Exception:
            continue
    if submit:
        await submit.click()
    else:
        await page.keyboard.press("Enter")
    return True


async def main() -> None:
    email = _env_or_die("LINKEDIN_EMAIL")
    password = _env_or_die("LINKEDIN_PASSWORD")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright missing. Run: .venv/bin/pip install playwright && .venv/bin/playwright install chromium")
        sys.exit(1)

    # Reuse the auto-applier's stealth fingerprint so the login session matches
    # what the bot uses afterwards.
    from bot.human import launch_stealth_context

    Path("data").mkdir(exist_ok=True)
    Path("data/screenshots").mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state=None)
        page = await ctx.new_page()
        try:
            print(f"Loading {LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            await page.fill("input#username", email)
            await page.fill("input#password", password)
            await page.click("button[type='submit']")
            print("Submitted credentials. Watching for redirect...")

            # Wait up to LOGIN_TIMEOUT_SECONDS for either feed or 2FA
            deadline = time.time() + LOGIN_TIMEOUT_SECONDS
            while time.time() < deadline:
                await asyncio.sleep(1)
                cur = page.url or ""
                if FEED_MARKER in cur:
                    print("Login succeeded (landed on feed).")
                    break
                # Try 2FA handler each loop — if present, handle it then keep looking for feed
                if await _handle_2fa_if_present(page):
                    deadline = time.time() + LOGIN_TIMEOUT_SECONDS  # extend after solving 2FA
                    continue
            else:
                # Timed out waiting; capture the page so we can inspect what blocked us
                fallback = "data/screenshots/linkedin_login_blocked.png"
                Path(fallback).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=fallback, full_page=False)
                await _send_telegram_photo(
                    "LinkedIn login didn't reach the feed within 60s. Screenshot attached — likely a CAPTCHA, "
                    "checkpoint, or wrong password. Check and re-run.",
                    fallback,
                )
                await browser.close()
                sys.exit(2)

            await asyncio.sleep(2)
            await ctx.storage_state(path=AUTH_STATE_PATH)
            print(f"Saved auth state -> {AUTH_STATE_PATH}")

            # Friendly success ping
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if token and chat_id:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data={"chat_id": chat_id, "text": "LinkedIn login saved. Searches will run on the next 30-min poll cycle."},
                        timeout=10,
                    )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
