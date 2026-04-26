#!/usr/bin/env python3
"""
Opens headed Chromium so you can log in to Handshake.
Saves auth state to data/handshake_auth.json on successful login.
Run with: DISPLAY=:0 python setup/handshake_login.py

Requires a Handshake account with a .edu email or alumni access.
"""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

LOGIN_URL = "https://app.joinhandshake.com/login"
AUTH_STATE_PATH = os.getenv("HANDSHAKE_AUTH_STATE", "data/handshake_auth.json")
TIMEOUT_SECONDS = 180

_SUCCESS_MARKERS = [
    "app.joinhandshake.com/home",
    "app.joinhandshake.com/edu",
    "app.joinhandshake.com/postings",
    "app.joinhandshake.com/students",
]


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    print("Handshake Login Setup")
    print("─────────────────────")
    print("You need a Handshake account with a .edu email or alumni access.")
    print("If you are a recent grad, use your university alumni login option.")
    print("")
    print("Opening Handshake login page...")
    print("You have " + str(TIMEOUT_SECONDS) + " seconds to log in (including 2FA/SSO).")
    print("The window closes automatically once login is detected.")
    print("")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)

        for _ in range(TIMEOUT_SECONDS * 2):
            await asyncio.sleep(0.5)
            if any(marker in page.url for marker in _SUCCESS_MARKERS):
                break
        else:
            print("Timed out waiting for login. Please try again.")
            await browser.close()
            sys.exit(1)

        await asyncio.sleep(2)
        await ctx.storage_state(path=AUTH_STATE_PATH)
        await browser.close()

    print("")
    print("Handshake auth saved to: " + AUTH_STATE_PATH)
    print("Handshake jobs will now appear in /queue on the next poll cycle.")


if __name__ == "__main__":
    asyncio.run(main())
