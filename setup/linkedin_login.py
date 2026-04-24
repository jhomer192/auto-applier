#!/usr/bin/env python3
"""
Opens a headed Chromium browser so you can log in to LinkedIn.
Saves auth state to data/linkedin_auth.json when login is detected.
Run with: DISPLAY=:0 python setup/linkedin_login.py
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_STATE_PATH = "data/linkedin_auth.json"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "linkedin.com/feed"
TIMEOUT_SECONDS = 180


async def main() -> None:
    Path("data").mkdir(exist_ok=True)

    print("Opening LinkedIn login page...")
    print(f"You have {TIMEOUT_SECONDS} seconds to log in (including 2FA if needed).")
    print("The window will close automatically once login is detected.
")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        await page.goto(LOGIN_URL)

        # Poll until we see the feed URL (login complete) or timeout
        for _ in range(TIMEOUT_SECONDS * 2):  # check every 0.5s
            await asyncio.sleep(0.5)
            if FEED_URL in page.url:
                break
        else:
            print("Timed out waiting for login. Please try again.")
            await browser.close()
            sys.exit(1)

        # Give page a moment to fully load session cookies
        await asyncio.sleep(2)
        await ctx.storage_state(path=AUTH_STATE_PATH)
        await browser.close()

    print(f"
LinkedIn auth state saved to: {AUTH_STATE_PATH}")
    print("You're now set up for LinkedIn Easy Apply.")


if __name__ == "__main__":
    asyncio.run(main())
