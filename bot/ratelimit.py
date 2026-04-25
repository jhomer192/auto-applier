"""Application rate limiter.

All limits default to 0 (disabled). Set via /prefs if you want pacing.
When both gap and cap are 0, enforce_rate_limit is a no-op.
"""
import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MIN_GAP_MINUTES: int = 0   # 0 = no gap
DEFAULT_MAX_GAP_MINUTES: int = 0   # 0 = no gap
DEFAULT_DAILY_CAP: int = 0         # 0 = no cap


class RateLimitExceeded(Exception):
    """Raised when the daily cap is reached."""


async def enforce_rate_limit(
    db,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
    max_gap_minutes: int = DEFAULT_MAX_GAP_MINUTES,
    daily_cap: int = DEFAULT_DAILY_CAP,
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    """Block until it is safe to submit the next application.

    All limits are opt-in — when daily_cap=0 and gap=0 this is a no-op.

    Args:
        db: ApplicationDB instance.
        min_gap_minutes: Minimum minutes between submissions (0 = no wait).
        max_gap_minutes: Upper bound for randomised gap (0 = no wait).
        daily_cap: Max successful applications per day (0 = unlimited).
        notify: Optional async callback for wait-status messages.
    """
    now = datetime.now(timezone.utc)

    # --- Daily cap (skip if 0) ---
    if daily_cap > 0:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        applied_today = await db.count_applied_today(today_start)
        if applied_today >= daily_cap:
            raise RateLimitExceeded(
                f"Daily cap of {daily_cap} applications reached. Will resume tomorrow."
            )

    # --- Time gap (skip if both are 0) ---
    if min_gap_minutes <= 0 and max_gap_minutes <= 0:
        return

    recent = await db.get_recent(limit=50)
    last_applied_str = next(
        (r.applied_at for r in recent if r.status == "applied" and r.applied_at),
        None,
    )
    if not last_applied_str:
        return

    try:
        last_dt = datetime.fromisoformat(last_applied_str)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return

    lo = max(min_gap_minutes, 0)
    hi = max(max_gap_minutes, lo)
    target_gap = random.uniform(lo * 60, hi * 60)
    wait_secs = round((last_dt + timedelta(seconds=target_gap) - now).total_seconds())

    if wait_secs <= 0:
        return

    logger.info("Rate limiter: waiting %ds before next application", wait_secs)
    if notify:
        mins, secs = divmod(wait_secs, 60)
        await notify(f"Pacing: waiting {'{}m {}s'.format(mins,secs) if mins else '{}s'.format(secs)} before next submission.")

    await asyncio.sleep(wait_secs)
