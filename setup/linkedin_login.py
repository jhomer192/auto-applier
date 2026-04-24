#!/usr/bin/env python3
"""
Opens headed Chromium so you can log in to LinkedIn.
Saves auth state to data/linkedin_auth.json on successful login.
Run with: DISPLAY=:0 python setup/linkedin_login.py
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_STATE_PATH = "data/linkedin_auth.json"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_MARKER = "linkedin.com/feed"
TIMEOUT_SECONDS = 180


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    print("Opening LinkedIn login page...")
    print("You have " + str(TIMEOUT_SECONDS) + " seconds to log in (including 2FA).")
    print("The window closes automatically once login is detected.")
    print("")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)

        for _ in range(TIMEOUT_SECONDS * 2):
            await asyncio.sleep(0.5)
            if FEED_MARKER in page.url:
                break
        else:
            print("Timed out waiting for login. Please try again.")
            await browser.close()
            sys.exit(1)

        await asyncio.sleep(2)
        await ctx.storage_state(path=AUTH_STATE_PATH)
        await browser.close()

    print("")
    print("LinkedIn auth state saved to: " + AUTH_STATE_PATH)
    print("You are now set up for LinkedIn Easy Apply.")


if __name__ == "__main__":
    asyncio.run(main())
