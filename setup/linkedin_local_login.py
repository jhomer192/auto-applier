#!/usr/bin/env python3
"""
Run this on your LOCAL machine (Mac/PC), not the VPS.

Opens a Chromium window, you log in to LinkedIn (including 2FA if needed),
press Enter in the terminal when done, and we save your authenticated session
to linkedin_auth.json. You then scp that file to the VPS:

    scp linkedin_auth.json claude-server:/root/auto-applier/data/linkedin_auth.json

Prereq (one-time on your local machine):
    pip install playwright
    python -m playwright install chromium

Then:
    python setup/linkedin_local_login.py
"""
import asyncio
import sys
from pathlib import Path

OUTPUT = "linkedin_auth.json"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_MARKER = "linkedin.com/feed"


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed. Run:")
        print("  pip install playwright")
        print("  python -m playwright install chromium")
        sys.exit(1)

    print("Opening Chromium. Log in to LinkedIn (including any 2FA challenge).")
    print("This window will close itself once you're logged in (URL contains /feed).")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)

        # Wait up to 5 minutes for the user to land on the feed
        for _ in range(600):
            await asyncio.sleep(0.5)
            try:
                if FEED_MARKER in (page.url or ""):
                    break
            except Exception:
                pass
        else:
            print("Timed out (5 min). Re-run when you're ready to log in.")
            await browser.close()
            sys.exit(1)

        await asyncio.sleep(2)  # let the feed settle / cookies finalize
        await ctx.storage_state(path=OUTPUT)
        await browser.close()

    out_abs = Path(OUTPUT).resolve()
    print()
    print(f"Saved auth state: {out_abs}")
    print()
    print("Now copy it to the VPS:")
    print(f"  scp {out_abs} claude-server:/root/auto-applier/data/linkedin_auth.json")
    print()
    print("After scp, the auto-applier will pick it up on the next 30-min poll cycle.")
    print("To trigger a search immediately, send `run my searches` to the bot in Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
