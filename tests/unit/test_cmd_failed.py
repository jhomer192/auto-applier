"""Tests for the /failed Telegram command.

Counts of failed jobs were already visible in /queue and /report (audit fix #3).
The /failed command surfaces the per-job detail — what went wrong, how many
attempts, and when the next retry is — so a recurring failure pattern is
diagnosable without dropping into the DB.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import telegram_bot
from bot.models import QueuedJob


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    # Pinned 'now' so retry math is deterministic across tests.
    return datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ctx(failed_jobs: list[QueuedJob]) -> MagicMock:
    db = MagicMock()
    db.get_failed_jobs = AsyncMock(return_value=failed_jobs)
    ctx = MagicMock()
    ctx.bot_data = {"db": db, "authorized_user_id": 1}
    return ctx


def _update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    return update


# ---------------------------------------------------------------------------
# _failed_retry_status — the 'when does this retry?' label
# ---------------------------------------------------------------------------


def test_retry_status_permanent_at_max_attempts():
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=3, last_error="boom",
        last_attempted_at=_now().isoformat(),
    )
    assert telegram_bot._failed_retry_status(job, _now()) == "permanent (no further retries)"


def test_retry_status_ready_when_no_last_attempt():
    """Defensive: a failed job with no timestamp must not be reported as 'in -inf hours'."""
    job = QueuedJob(url="https://x", title="t", company="c", attempts=1, last_attempted_at=None)
    assert "ready for retry now" in telegram_bot._failed_retry_status(job, _now())


def test_retry_status_ready_when_cooldown_elapsed():
    last = _now() - timedelta(hours=2)
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=1, last_attempted_at=last.isoformat(),
    )
    assert "ready for retry now" in telegram_bot._failed_retry_status(job, _now())


def test_retry_status_minutes_remaining():
    last = _now() - timedelta(minutes=20)  # 40 min until retry
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=1, last_attempted_at=last.isoformat(),
    )
    status = telegram_bot._failed_retry_status(job, _now())
    assert "retry in ~40m" == status


def test_retry_status_hours_remaining():
    """Cooldown values pulled from cmd_failed are 1h; future-proof if it ever
    grows beyond an hour (e.g. configurable)."""
    last = _now() - timedelta(minutes=5)  # 55 min until retry — formatted as ~55m
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=1, last_attempted_at=last.isoformat(),
    )
    status = telegram_bot._failed_retry_status(job, _now())
    assert "retry in ~55m" == status


def test_retry_status_naive_timestamp_treated_as_utc():
    """SQLite TIMESTAMP columns sometimes round-trip without tzinfo; the helper
    must not crash on naive datetimes."""
    last = (_now() - timedelta(minutes=30)).replace(tzinfo=None)
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=1, last_attempted_at=last.isoformat(),
    )
    # Should not raise; should produce a sane label.
    status = telegram_bot._failed_retry_status(job, _now())
    assert "retry in" in status or "ready for retry" in status


def test_retry_status_garbage_timestamp_treated_as_ready():
    """A corrupt timestamp must not crash the command — treat it as ready."""
    job = QueuedJob(
        url="https://x", title="t", company="c",
        attempts=1, last_attempted_at="not-a-date",
    )
    assert "ready for retry now" in telegram_bot._failed_retry_status(job, _now())


# ---------------------------------------------------------------------------
# cmd_failed — empty / populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_failed_empty_queue():
    update = _update()
    ctx = _ctx([])
    await telegram_bot.cmd_failed(update, ctx)
    update.message.reply_text.assert_called_once()
    msg = update.message.reply_text.call_args.args[0]
    assert "No failed jobs" in msg


@pytest.mark.asyncio
async def test_cmd_failed_lists_jobs_with_error_and_status():
    job = QueuedJob(
        id=42, url="https://greenhouse.io/co/job/1",
        title="Senior Engineer", company="Acme Corp",
        status="failed", attempts=1,
        last_error="Playwright timeout: navigating to ...",
        last_attempted_at=(_now() - timedelta(minutes=20)).isoformat(),
    )
    update = _update()
    ctx = _ctx([job])
    await telegram_bot.cmd_failed(update, ctx)

    msg = update.message.reply_text.call_args.args[0]
    assert "Failed jobs (1)" in msg
    assert "Senior Engineer" in msg
    assert "Acme Corp" in msg
    assert "Playwright timeout" in msg
    assert "1/3" in msg  # attempts
    assert "retry in" in msg
    assert "https://greenhouse.io/co/job/1" in msg


@pytest.mark.asyncio
async def test_cmd_failed_truncates_long_error():
    long_err = "x" * 500
    job = QueuedJob(
        id=1, url="https://x", title="t", company="c",
        status="failed", attempts=1, last_error=long_err,
    )
    update = _update()
    ctx = _ctx([job])
    await telegram_bot.cmd_failed(update, ctx)

    msg = update.message.reply_text.call_args.args[0]
    # 120-char preview cap, ending with "..."
    assert "..." in msg
    assert long_err not in msg  # full string never appears


@pytest.mark.asyncio
async def test_cmd_failed_caps_at_twenty_with_overflow_note():
    jobs = [
        QueuedJob(id=i, url=f"https://x/{i}", title=f"t{i}", company="c",
                  status="failed", attempts=1, last_error="err")
        for i in range(25)
    ]
    update = _update()
    ctx = _ctx(jobs)
    await telegram_bot.cmd_failed(update, ctx)

    msg = update.message.reply_text.call_args.args[0]
    assert "Failed jobs (25)" in msg
    # 20th item (index 19, title t19) is the last one rendered.
    assert "20. t19 @ c" in msg
    # 21st item and beyond must not be rendered individually.
    assert "21. " not in msg
    assert "t20 @ c" not in msg
    assert "...and 5 more" in msg


@pytest.mark.asyncio
async def test_cmd_failed_disables_link_preview():
    """Telegram auto-expands the first URL — 20 huge previews would be unusable."""
    job = QueuedJob(
        id=1, url="https://x", title="t", company="c",
        status="failed", attempts=0, last_error="err",
    )
    update = _update()
    ctx = _ctx([job])
    await telegram_bot.cmd_failed(update, ctx)

    kwargs = update.message.reply_text.call_args.kwargs
    assert kwargs.get("disable_web_page_preview") is True


@pytest.mark.asyncio
async def test_cmd_failed_handles_missing_error_field():
    """An older row pre-migration may have last_error=''; render '—' not 'None'."""
    job = QueuedJob(
        id=1, url="https://x", title="t", company="c",
        status="failed", attempts=1, last_error="",
    )
    update = _update()
    ctx = _ctx([job])
    await telegram_bot.cmd_failed(update, ctx)

    msg = update.message.reply_text.call_args.args[0]
    assert "—" in msg
    assert "None" not in msg  # don't leak the Python repr
