"""Tests for the Telegram message chunking helper.

Telegram caps a single message at 4096 chars; resumes/cover letters/audit reports
routinely exceed this. `_split_for_telegram` must split on paragraph then line
boundaries before falling back to a hard cut, and `reply_chunked` must dispatch
the chunks with a "(part N/M)" prefix so the user knows the message continues.
"""
from unittest.mock import AsyncMock

import pytest

from bot.telegram_bot import (
    TELEGRAM_MAX_CHARS,
    _split_for_telegram,
    reply_chunked,
    send_chunked,
)


def test_short_text_returns_single_chunk():
    chunks = _split_for_telegram("hello world")
    assert chunks == ["hello world"]


def test_long_text_chunks_under_limit():
    text = "a" * (TELEGRAM_MAX_CHARS * 3)  # ~12k chars, no break points
    chunks = _split_for_telegram(text)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c) <= TELEGRAM_MAX_CHARS


def test_paragraph_break_preferred_over_line_break():
    # Build text large enough to require a split, with a paragraph break in the cut window.
    block = "x" * 1800
    text = f"{block}\n\nPARAGRAPH-MARKER\n{block}\n\nfinal"  # ~3650 chars total
    chunks = _split_for_telegram(text, limit=2500)
    assert len(chunks) >= 2
    # The split should land on the \n\n, so the marker starts a later chunk.
    assert any(c.startswith("PARAGRAPH-MARKER") for c in chunks[1:]), (
        f"Expected PARAGRAPH-MARKER to start a later chunk, got: {[c[:30] for c in chunks]}"
    )


def test_line_break_used_when_no_paragraph_break():
    # No \n\n at all — should still split on a single \n in the cut window.
    block = "y" * 1800
    text = f"{block}\nLINE-MARKER\n{block}\nfinal"  # ~3640 chars
    chunks = _split_for_telegram(text, limit=2500)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 2500


def test_hard_cut_when_no_break_at_all():
    text = "z" * 5000
    chunks = _split_for_telegram(text, limit=2000)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


def test_chunks_concatenate_back_to_original_with_separators():
    # Round-trip property: chunks joined by their dropped separator produce the original.
    # With paragraph splits we drop "\n\n", so we can't always exactly reconstruct, but
    # we can verify the content tokens are preserved.
    text = "alpha\n\nbeta\n\n" + ("gamma " * 1000) + "\n\ndelta"
    chunks = _split_for_telegram(text, limit=2000)
    rejoined = "".join(chunks)
    # Every token should still appear, even if separator chars are dropped.
    for token in ("alpha", "beta", "gamma", "delta"):
        assert token in rejoined


@pytest.mark.asyncio
async def test_reply_chunked_single_message_no_prefix():
    message = AsyncMock()
    await reply_chunked(message, "short message")
    message.reply_text.assert_awaited_once_with("short message", parse_mode=None)


@pytest.mark.asyncio
async def test_reply_chunked_multi_message_adds_part_prefix():
    message = AsyncMock()
    text = "x" * (TELEGRAM_MAX_CHARS * 2 + 100)
    await reply_chunked(message, text)
    # At least 3 chunks, each prefixed with "(part N/M)"
    assert message.reply_text.await_count >= 3
    for i, call in enumerate(message.reply_text.await_args_list, start=1):
        sent_text = call.args[0]
        assert sent_text.startswith(f"(part {i}/")


@pytest.mark.asyncio
async def test_reply_chunked_preserves_parse_mode():
    message = AsyncMock()
    await reply_chunked(message, "*bold*", parse_mode="Markdown")
    message.reply_text.assert_awaited_once_with("*bold*", parse_mode="Markdown")


@pytest.mark.asyncio
async def test_send_chunked_single_message():
    bot = AsyncMock()
    await send_chunked(bot, 12345, "hello")
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="hello", parse_mode=None)


@pytest.mark.asyncio
async def test_send_chunked_multi_message_adds_prefix():
    bot = AsyncMock()
    text = "y" * (TELEGRAM_MAX_CHARS * 2 + 50)
    await send_chunked(bot, 99, text)
    assert bot.send_message.await_count >= 3
    for i, call in enumerate(bot.send_message.await_args_list, start=1):
        assert call.kwargs["text"].startswith(f"(part {i}/")
        assert call.kwargs["chat_id"] == 99
