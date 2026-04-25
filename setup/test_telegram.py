#!/usr/bin/env python3
"""Sends a test message to verify Telegram bot token and chat ID are correct."""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import Bot


async def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        print("ERROR: TELEGRAM_CHAT_ID must be a plain integer (e.g. 123456789). Check your .env.")
        sys.exit(1)

    try:
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=chat_id_int,
            text=(
                "Setup complete! Your auto job applier is ready.\n\n"
                "Send a job URL to apply:\n"
                "- linkedin.com/jobs/view/...\n"
                "- boards.greenhouse.io/...\n"
                "- jobs.lever.co/..."
            ),
        )
        print("Test message sent successfully!")
    except Exception as e:
        print("ERROR: " + str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
