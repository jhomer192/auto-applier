"""CAPTCHA detection, telemetry, and graceful-abort fallback.

Layer 1 of the four-layer plan in .claude/tasks/captcha-solver.md. We don't
solve anything yet — every hit is detected, screenshotted, logged to JSONL,
and broadcast to Telegram so the operator knows the bot got blocked. The
calling site is expected to abort the current navigation when handle()
returns False.

Decision on Layer 2/3 (in-house audio solver / paid CapSolver) is gated on
what shows up in data/captcha_log.jsonl. Read the spec before extending.

Telegram notifications use TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID directly
from the environment so callers in scraper / search modules don't need to
plumb the PTB Bot reference through.
"""
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp
from playwright.async_api import Page

logger = logging.getLogger(__name__)

LOG_PATH = "data/captcha_log.jsonl"
SCREENSHOT_DIR = "data/screenshots"


class CaptchaKind(str, Enum):
    NONE = "none"
    RECAPTCHA_V2 = "recaptcha_v2"            # image grid challenge
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"                   # Cloudflare
    ARKOSE = "arkose"                         # FunCaptcha — LinkedIn's main one
    LINKEDIN_CHECKPOINT = "li_checkpoint"     # phone/email verify, not a captcha
    UNKNOWN_CHALLENGE = "unknown_challenge"   # something blocked us, type unclear


@dataclass
class DetectionResult:
    kind: CaptchaKind
    detail: str = ""


async def detect(page: Page) -> DetectionResult:
    """Probe the current page for known CAPTCHA / challenge surfaces.

    Cheap — runs DOM queries, no network. Safe to call after every navigation.
    """
    try:
        url = page.url or ""
    except Exception:
        url = ""

    if "/checkpoint/challenge" in url or "/checkpoint/" in url:
        return DetectionResult(CaptchaKind.LINKEDIN_CHECKPOINT, url)

    iframe_probes = [
        ('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]', CaptchaKind.ARKOSE),
        ('iframe[src*="hcaptcha.com"]', CaptchaKind.HCAPTCHA),
        ('iframe[src*="challenges.cloudflare.com"]', CaptchaKind.TURNSTILE),
        ('iframe[src*="recaptcha/api2"]', CaptchaKind.RECAPTCHA_V2),
    ]
    for selector, kind in iframe_probes:
        try:
            handle = await page.query_selector(selector)
        except Exception:
            handle = None
        if handle:
            try:
                src = await handle.get_attribute("src") or ""
            except Exception:
                src = ""
            return DetectionResult(kind, src)

    try:
        title = (await page.title()) or ""
    except Exception:
        title = ""
    if any(token in title.lower() for token in ("verify you are human", "security check", "are you a robot")):
        return DetectionResult(CaptchaKind.UNKNOWN_CHALLENGE, title)

    return DetectionResult(CaptchaKind.NONE)


async def screenshot(page: Page, prefix: str = "captcha") -> Optional[str]:
    Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
    path = f"{SCREENSHOT_DIR}/{prefix}_{int(time.time())}.png"
    try:
        await page.screenshot(path=path, full_page=False)
        return path
    except Exception as e:
        logger.warning("captcha screenshot failed: %s", e)
        return None


def log_hit(kind: CaptchaKind, url: str, screenshot_path: Optional[str], detail: str = "") -> None:
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "kind": kind.value,
        "url": url,
        "screenshot": screenshot_path or "",
        "detail": detail[:200],
    }
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning("captcha log write failed: %s", e)


async def _notify_telegram(caption: str, photo_path: Optional[str]) -> None:
    """Send a CAPTCHA alert to the operator via env-configured Telegram bot.

    Silent no-op if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID isn't set.
    Uses aiohttp directly so callers don't need a PTB Bot reference.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    base = f"https://api.telegram.org/bot{token}"
    try:
        async with aiohttp.ClientSession() as session:
            if photo_path and Path(photo_path).exists():
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("caption", caption)
                form.add_field("photo", open(photo_path, "rb"), filename=Path(photo_path).name)
                async with session.post(f"{base}/sendPhoto", data=form, timeout=15) as r:
                    if r.status != 200:
                        logger.warning("captcha telegram sendPhoto status=%s", r.status)
            else:
                async with session.post(
                    f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": caption},
                    timeout=15,
                ) as r:
                    if r.status != 200:
                        logger.warning("captcha telegram sendMessage status=%s", r.status)
    except Exception as e:
        logger.warning("captcha telegram notify failed: %s", e)


async def handle(page: Page, *, context_label: str = "") -> bool:
    """Detect, log, notify. Return True to proceed, False to abort the caller.

    For Layer 1 we never attempt to solve. A False return is the signal to
    abort the current search / application gracefully — the caller is
    responsible for closing the browser context.

    Notification target comes from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    in the environment.
    """
    result = await detect(page)
    if result.kind == CaptchaKind.NONE:
        return True

    try:
        url = page.url or ""
    except Exception:
        url = ""

    shot = await screenshot(page, prefix=f"captcha_{result.kind.value}")
    log_hit(result.kind, url, shot, detail=result.detail)
    logger.warning(
        "CAPTCHA hit (%s) on %s — context=%s screenshot=%s",
        result.kind.value, url, context_label, shot,
    )

    caption = (
        f"⚠ CAPTCHA blocked the bot\n"
        f"Kind: {result.kind.value}\n"
        f"Where: {context_label or url[:80]}\n"
        f"Current job is being skipped. /captcha for stats."
    )
    await _notify_telegram(caption, shot)
    return False


def stats(log_path: str = LOG_PATH, days: int = 7) -> dict:
    """Return total + per-kind counts over the last `days` days."""
    cutoff = int(time.time()) - days * 86400
    counts: Counter = Counter()
    total = 0
    if not Path(log_path).exists():
        return {"total": 0, "by_kind": {}, "days": days}
    try:
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("ts", 0) < cutoff:
                    continue
                counts[entry.get("kind", "unknown")] += 1
                total += 1
    except Exception as e:
        logger.warning("captcha stats read failed: %s", e)
    return {"total": total, "by_kind": dict(counts), "days": days}
