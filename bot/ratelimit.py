"""Application rate limiter.

Enforces:
  - Minimum randomised gap between submissions (default 4–8 minutes)
  - Daily application cap (default 30/day)

Uses the existing applications table — no new state needed. The last
applied_at timestamp and today's applied count are read from the DB at
check time so the limiter is accurate even across process restarts.
"""
import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Defaults (overridden by job_preferences if set)
DEFAULT_MIN_GAP_MINUTES: int = 4
DEFAULT_MAX_GAP_MINUTES: int = 8
DEFAULT_DAILY_CAP: int = 30


class RateLimitExceeded(Exception):
    """Raised when the daily cap is already reached."""


async def enforce_rate_limit(
    db,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
    max_gap_minutes: int = DEFAULT_MAX_GAP_MINUTES,
    daily_cap: int = DEFAULT_DAILY_CAP,
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    """Block until it is safe to submit the next application.

    Steps:
    1. Count today's successful applications. Raise RateLimitExceeded if >= cap.
    2. Find the last successful application timestamp.
    3. Choose a random target gap in [min_gap, max_gap] minutes.
    4. Sleep for however long is left to reach that gap (0 if already past it).

    Args:
        db: ApplicationDB instance.
        min_gap_minutes: Minimum minutes to wait since last submission.
        max_gap_minutes: Upper bound for the randomised gap.
        daily_cap: Maximum successful applications per UTC calendar day.
        notify: Optional async callback called with a wait-status message.
    """
    now = datetime.now(timezone.utc)

    # --- Daily cap ---
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    recent = await db.get_recent(limit=daily_cap + 20)
    applied_today = sum(
        1 for r in recent
        if r.status == "applied"
        and r.applied_at
        and r.applied_at >= today_start
    )
    if applied_today >= daily_cap:
        raise RateLimitExceeded(
            f"Daily cap of {daily_cap} applications reached. Will resume tomorrow."
        )

    # --- Time gap ---
    last_applied_str = next(
        (r.applied_at for r in recent if r.status == "applied" and r.applied_at),
        None,
    )
    if not last_applied_str:
        return  # No prior applications — submit immediately

    try:
        last_dt = datetime.fromisoformat(last_applied_str)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return  # Unparseable timestamp — don't block

    target_gap = random.uniform(min_gap_minutes * 60, max_gap_minutes * 60)
    earliest_next = last_dt + timedelta(seconds=target_gap)
    wait_secs = (earliest_next - now).total_seconds()

    if wait_secs <= 0:
        return  # Gap already passed

    wait_secs = round(wait_secs)
    logger.info("Rate limiter: waiting %ds before next application", wait_secs)

    if notify:
        mins, secs = divmod(wait_secs, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        await notify(
            f"Pacing: waiting {time_str} before submitting to avoid detection."
        )

    await asyncio.sleep(wait_secs)
