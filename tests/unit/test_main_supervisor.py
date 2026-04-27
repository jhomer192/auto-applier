"""Tests for the background-loop watchdog (audit fix #8).

Each background poll loop (inbox, search, sources) runs under ``_supervise``.
If a loop's outer ``while True`` ever escapes via an uncaught exception, the
supervisor catches it, logs, sends a Telegram alert, and restarts with
exponential backoff (capped). Without this, the bot would look healthy while
half its features had silently died.
"""
from unittest.mock import AsyncMock, MagicMock

import asyncio
import pytest

from bot import main as bot_main


def _app(authorized_user_id: int | None = 12345) -> MagicMock:
    """Minimal stand-in for the python-telegram-bot Application object."""
    app = MagicMock()
    app.bot_data = {}
    if authorized_user_id is not None:
        app.bot_data["authorized_user_id"] = authorized_user_id
    app.bot.send_message = AsyncMock()
    return app


# ── Restart on crash ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervise_restarts_after_uncaught_exception(monkeypatch):
    """An exception escaping the loop must be caught and the loop re-invoked."""
    # Sleep instantly to keep the test fast.
    monkeypatch.setattr(bot_main.asyncio, "sleep", AsyncMock())

    app = _app()
    call_count = 0

    async def loop_fn():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError(f"boom #{call_count}")
        return  # 3rd call returns cleanly, supervisor exits

    await bot_main._supervise(app, "test-loop", loop_fn)
    assert call_count == 3


@pytest.mark.asyncio
async def test_supervise_alerts_user_on_crash(monkeypatch):
    """Each crash must trigger a Telegram alert to the authorized user."""
    monkeypatch.setattr(bot_main.asyncio, "sleep", AsyncMock())

    app = _app(authorized_user_id=12345)
    calls = 0

    async def loop_fn():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("boom")
        return

    await bot_main._supervise(app, "test-loop", loop_fn)

    # send_message called once for the single crash before the clean exit
    assert app.bot.send_message.await_count == 1
    kwargs = app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert "test-loop" in kwargs["text"]
    assert "crashed" in kwargs["text"]


@pytest.mark.asyncio
async def test_supervise_exponential_backoff(monkeypatch):
    """Backoff doubles after each failure, capped at SUPERVISE_MAX_BACKOFF."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot_main.asyncio, "sleep", fake_sleep)

    app = _app()
    crashes = 0
    target_crashes = 4

    async def loop_fn():
        nonlocal crashes
        crashes += 1
        if crashes <= target_crashes:
            raise RuntimeError("boom")
        return

    await bot_main._supervise(app, "test-loop", loop_fn)

    # 4 crashes → 4 sleeps. First is INITIAL_BACKOFF (5), then 10, 20, 40.
    assert len(sleeps) == target_crashes
    assert sleeps[0] == bot_main.SUPERVISE_INITIAL_BACKOFF
    assert sleeps[1] == 10
    assert sleeps[2] == 20
    assert sleeps[3] == 40


@pytest.mark.asyncio
async def test_supervise_backoff_caps_at_max(monkeypatch):
    """After many crashes the sleep stops growing past SUPERVISE_MAX_BACKOFF."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot_main.asyncio, "sleep", fake_sleep)

    app = _app()
    crashes = 0
    # crash enough times to exceed the cap (5 → 10 → 20 → 40 → 80 → 160 → 300)
    target_crashes = 10

    async def loop_fn():
        nonlocal crashes
        crashes += 1
        if crashes <= target_crashes:
            raise RuntimeError("boom")
        return

    await bot_main._supervise(app, "test-loop", loop_fn)

    # All later sleeps must be <= the cap, and at least one must equal it.
    assert all(s <= bot_main.SUPERVISE_MAX_BACKOFF for s in sleeps)
    assert sleeps[-1] == bot_main.SUPERVISE_MAX_BACKOFF


# ── Cancellation must propagate ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervise_propagates_cancellation():
    """Shutdown sends CancelledError; supervisor must NOT swallow it."""
    app = _app()

    async def loop_fn():
        # Block forever so the outer cancellation lands here.
        await asyncio.Event().wait()

    task = asyncio.create_task(bot_main._supervise(app, "test-loop", loop_fn))
    # Yield once so the task starts the loop_fn
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Clean exit when loop returns normally ────────────────────────────────────


@pytest.mark.asyncio
async def test_supervise_returns_when_loop_returns(monkeypatch):
    """If the loop returns without raising, the supervisor exits — no restart."""
    monkeypatch.setattr(bot_main.asyncio, "sleep", AsyncMock())

    app = _app()
    calls = 0

    async def loop_fn():
        nonlocal calls
        calls += 1
        return

    await bot_main._supervise(app, "test-loop", loop_fn)
    assert calls == 1
    app.bot.send_message.assert_not_awaited()


# ── Alert failure must not re-crash the supervisor ───────────────────────────


@pytest.mark.asyncio
async def test_supervise_survives_failed_alert(monkeypatch):
    """If send_message itself raises (e.g. Telegram down), the supervisor still
    keeps trying to restart the loop instead of dying with the alert."""
    monkeypatch.setattr(bot_main.asyncio, "sleep", AsyncMock())

    app = _app()
    app.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

    calls = 0

    async def loop_fn():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("boom")
        return

    # Must complete without raising despite the failed alert.
    await bot_main._supervise(app, "test-loop", loop_fn)
    assert calls == 2


# ── Pass-through args/kwargs ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervise_forwards_args_and_kwargs():
    """The supervisor must transparently pass through positional and keyword args."""
    seen = {}

    async def loop_fn(a, b, *, kw):
        seen["a"] = a
        seen["b"] = b
        seen["kw"] = kw
        return

    app = _app()
    await bot_main._supervise(app, "test-loop", loop_fn, "x", "y", kw="z")
    assert seen == {"a": "x", "b": "y", "kw": "z"}


# ── Heartbeat ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_logs_on_each_interval(monkeypatch, caplog):
    """The heartbeat must emit one INFO log per interval."""
    sleep_calls = 0

    async def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            # End the heartbeat loop so the test can assert.
            raise asyncio.CancelledError()

    monkeypatch.setattr(bot_main.asyncio, "sleep", fake_sleep)

    import logging
    caplog.set_level(logging.INFO, logger=bot_main.logger.name)

    with pytest.raises(asyncio.CancelledError):
        await bot_main._heartbeat("test-loop", interval=0)

    heartbeats = [r for r in caplog.records if "heartbeat" in r.message]
    assert len(heartbeats) >= 2
    assert any("test-loop" in r.message for r in heartbeats)
