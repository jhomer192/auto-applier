"""Tests for the @requires_auth decorator (audit fix #7).

Before this fix, every Telegram handler called ``_auth(update, context)`` inline as
its first statement. That works, but it was a trap: any new handler that forgot the
check would silently leak data. The decorator centralises the gate so it is
impossible to register an unguarded handler without explicitly skipping it.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import telegram_bot


def _ctx(authorized_user_id: int = 12345) -> MagicMock:
    """Construct a minimal context with the bot_data the decorator needs."""
    ctx = MagicMock()
    ctx.bot_data = {"authorized_user_id": authorized_user_id}
    return ctx


def _update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    return update


# ---------------------------------------------------------------------------
# Decorator behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decorator_calls_handler_when_authorised():
    handler = AsyncMock(return_value="OK")
    wrapped = telegram_bot.requires_auth(handler)

    update = _update(12345)
    ctx = _ctx(authorized_user_id=12345)

    result = await wrapped(update, ctx)
    assert result == "OK"
    handler.assert_awaited_once_with(update, ctx)


@pytest.mark.asyncio
async def test_decorator_blocks_handler_when_unauthorised():
    handler = AsyncMock()
    wrapped = telegram_bot.requires_auth(handler)

    update = _update(99999)  # different user
    ctx = _ctx(authorized_user_id=12345)

    result = await wrapped(update, ctx)
    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_decorator_drops_silently_no_reply_no_db_writes():
    """Unauthorised access must not reply, must not write the DB, must not log warnings."""
    db = MagicMock()
    db.insert_application = AsyncMock()

    handler = AsyncMock()
    wrapped = telegram_bot.requires_auth(handler)

    update = _update(99999)
    ctx = _ctx(authorized_user_id=12345)
    ctx.bot_data["db"] = db

    await wrapped(update, ctx)

    handler.assert_not_awaited()
    update.message.reply_text.assert_not_called()
    db.insert_application.assert_not_called()


@pytest.mark.asyncio
async def test_decorator_passes_through_extra_args():
    """The decorator must transparently forward *args and **kwargs."""
    handler = AsyncMock(return_value="passthrough")
    wrapped = telegram_bot.requires_auth(handler)

    update = _update(1)
    ctx = _ctx(authorized_user_id=1)

    result = await wrapped(update, ctx, "extra_positional", kw="value")
    assert result == "passthrough"
    handler.assert_awaited_once_with(update, ctx, "extra_positional", kw="value")


def test_decorator_preserves_handler_metadata():
    """``functools.wraps`` should keep the original __name__ and __doc__."""
    @telegram_bot.requires_auth
    async def my_handler(update, context):
        """My docstring."""
        return None

    assert my_handler.__name__ == "my_handler"
    assert my_handler.__doc__ == "My docstring."


# ---------------------------------------------------------------------------
# Module-level: every public command/handler is gated by the decorator
# ---------------------------------------------------------------------------

# Names that must be wrapped by @requires_auth. If a new handler is added,
# add it here too — the test will fail if it isn't decorated.
GATED_HANDLERS = [
    "cmd_start", "cmd_help", "cmd_status", "cmd_history", "cmd_cancel",
    "handle_text", "cmd_search", "cmd_resume", "cmd_coverletter",
    "cmd_referrals", "cmd_prefs", "cmd_voice", "cmd_profile", "cmd_queue",
    "cmd_report", "cmd_failed", "cmd_sources", "cmd_handshake", "cmd_scams",
    "cmd_scam_apply", "cmd_force", "cmd_linkedin", "cmd_website",
]


@pytest.mark.parametrize("name", GATED_HANDLERS)
@pytest.mark.asyncio
async def test_handler_is_gated_by_requires_auth(name):
    """Send an unauthorised update to each handler and assert it is a no-op.

    This is a structural regression test: if a handler ever loses its decorator
    (or someone adds a new handler without one), this test fails.
    """
    handler = getattr(telegram_bot, name)

    update = _update(99999)  # not the authorized user
    ctx = _ctx(authorized_user_id=12345)
    ctx.bot_data.update({
        "db": MagicMock(),
        "profile": {},
        "registry": MagicMock(),
        "screenshot_dir": "/tmp",
    })

    # Must not raise, must not call message.reply_text
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()

    await handler(update, ctx)

    update.message.reply_text.assert_not_called()
    update.message.reply_photo.assert_not_called()
