"""Discord transport seam for the auto-applier.

The bot's 25 command handlers + handle_text + conversation runtime were written
against python-telegram-bot: they take ``(update, context)`` and call
``update.message.reply_text`` / ``context.bot_data`` / ``context.bot.send_message``
etc. The background poll loops + apply pipeline take a PTB ``Application`` and call
``app.bot.send_message(chat_id=, text=, parse_mode=)`` / ``app.bot_data`` /
``app.create_task``.

Rather than rewrite all of that, this module provides three duck-typed shims so
the existing handlers/loops run UNCHANGED on discord.py:

  * ``Ctx``       — satisfies BOTH the ``update`` and ``context`` interfaces.
  * ``Messenger`` — a PTB-``Bot``-shaped facade over a single Discord channel
                    (chunks at 2000, swallows ``parse_mode``, caches sent
                    messages so ``edit_message_text`` works for the StatusBoard).
  * ``FakeApp``   — ``.bot`` / ``.bot_data`` / ``.create_task`` for the loops.

Keeping the Telegram handlers untouched also keeps ``bot/main.py`` runnable, so
rollback is just ``systemctl start auto-applier``.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any, Optional

import discord

logger = logging.getLogger("auto-applier-discord")

DISCORD_LIMIT = 2000


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split into <=limit chunks, preferring newline boundaries; hard-slice long lines."""
    text = text if text else ""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out or [""]


def _as_file(obj: Any, default_name: str = "file.bin") -> discord.File:
    if isinstance(obj, (bytes, bytearray)):
        return discord.File(io.BytesIO(bytes(obj)), filename=default_name)
    if hasattr(obj, "read"):  # open file handle
        # Pass default_name explicitly: discord.File would otherwise derive the
        # name from the handle's .name (e.g. a NamedTemporaryFile's tmpXXXX path),
        # dropping the caller's intended filename (e.g. jack-homer-site.html).
        return discord.File(obj, filename=default_name)
    return discord.File(str(obj))  # path


class _Handle:
    """Stands in for a Telegram Message return value (only .message_id is read)."""

    __slots__ = ("message_id", "message")

    def __init__(self, message: Optional[discord.Message]):
        self.message = message
        self.message_id = message.id if message is not None else None


class Messenger:
    """PTB-``telegram.Bot``-shaped facade bound to ONE Discord channel.

    Routing ignores ``chat_id`` (single channel); ``parse_mode`` is swallowed
    (Telegram-markdown ≈ Discord-markdown; the only divergence is single-``*``
    bold rendering as italic, and ``_``/``*`` inside paths/URLs parses as
    emphasis — accepted as cosmetic across all handler messages). Sent messages
    are cached by id so ``edit_message_text`` can drive the in-place StatusBoard
    via the retained ``discord.Message``; rapid edits are coalesced (see
    ``edit_message_text``) so a streaming tool loop never stalls on Discord's
    per-message edit rate-limit bucket.
    """

    EDIT_MIN_INTERVAL = 1.5  # seconds between edits to one message (Discord edit bucket)

    def __init__(self, channel: discord.abc.Messageable):
        self._ch = channel
        self._am = discord.AllowedMentions(everyone=False, roles=False, users=True)
        self._cache: "dict[int, discord.Message]" = {}
        self._pending_edits: "dict[int, str]" = {}
        self._edit_tasks: "dict[int, asyncio.Task]" = {}

    async def _send_text(self, text: str) -> Optional[discord.Message]:
        first: Optional[discord.Message] = None
        for c in chunk(text or "(empty)"):
            # suppress_embeds: every send here is a status/board/notification line.
            # Telegram passed disable_web_page_preview on the URL-heavy commands
            # (/failed, /referrals, the discovery batch); without this Discord
            # auto-embeds each job URL into a wall of link previews.
            m = await self._ch.send(c, allowed_mentions=self._am, suppress_embeds=True)
            if first is None:
                first = m
            self._cache[m.id] = m
            if len(self._cache) > 256:  # bound the cache
                self._cache.pop(next(iter(self._cache)))
        return first

    async def send_message(self, chat_id: Any = None, text: str = "", parse_mode: Any = None, **kw: Any) -> _Handle:
        return _Handle(await self._send_text(text))

    async def edit_message_text(self, chat_id: Any = None, message_id: Any = None, text: str = "", parse_mode: Any = None, **kw: Any) -> None:
        # message_id is None only when the StatusBoard's opening send failed and
        # never captured an id — there is nothing to edit, so do NOT fall back to
        # posting a fresh message (that would re-post the whole board every tick).
        if message_id is None:
            return
        if len(text) > DISCORD_LIMIT:
            text = text[: DISCORD_LIMIT - 24] + "\n…(truncated)"
        msg = self._cache.get(message_id)
        if msg is None:  # cached message evicted (256-bound) → post once so it isn't lost
            await self._send_text(text)
            return
        # LRU-touch: a message being actively edited must not become the oldest entry
        # and get evicted mid-turn (which would make every later edit re-post a fresh
        # board). Move it to the end so _send_text's FIFO eviction never claims it.
        self._cache[message_id] = self._cache.pop(message_id)
        # Coalesce streamed edits: discord.py SLEEPS on the per-message edit 429
        # bucket, so awaiting every StatusBoard tick inline would stall the tool
        # loop. Record the latest text and let one debounced writer per message
        # converge to it at <= one edit per EDIT_MIN_INTERVAL.
        self._pending_edits[message_id] = text
        if message_id not in self._edit_tasks:
            self._edit_tasks[message_id] = asyncio.create_task(self._drain_edits(message_id, msg))

    async def _drain_edits(self, message_id: int, msg: discord.Message) -> None:
        # Single writer per message. The while-check → finally-pop tail has no
        # await, so a concurrent edit_message_text either ran before the check
        # (re-fills _pending_edits → loop continues) or after the pop (spawns a
        # fresh drainer) — text can never be stranded.
        try:
            while message_id in self._pending_edits:
                text = self._pending_edits.pop(message_id)
                try:
                    await msg.edit(content=text or "…", allowed_mentions=self._am)
                except discord.HTTPException as e:
                    logger.debug("edit_message_text failed: %s", e)
                except Exception as e:  # noqa: BLE001
                    # A connection-level error (aiohttp ClientError / TimeoutError) is
                    # NOT an HTTPException. The text was already popped into the local
                    # above, so re-queue it (setdefault: don't clobber a newer snapshot)
                    # — otherwise a failed FINAL edit strands the board on a stale line.
                    logger.debug("edit_message_text error: %s", e)
                    self._pending_edits.setdefault(message_id, text)
                await asyncio.sleep(self.EDIT_MIN_INTERVAL)
        finally:
            self._edit_tasks.pop(message_id, None)

    async def _send_file(self, file: discord.File, caption: Any) -> None:
        # A caption over the 2000 limit would make channel.send raise and the file
        # would be lost with it. Send the file with an inline caption only when it
        # fits; otherwise send the file bare, then the caption as its own message(s).
        cap = caption or None
        if cap and len(cap) > DISCORD_LIMIT:
            await self._ch.send(file=file, allowed_mentions=self._am)
            await self._send_text(cap)
        else:
            await self._ch.send(content=cap, file=file, allowed_mentions=self._am, suppress_embeds=True)

    async def send_photo(self, chat_id: Any = None, photo: Any = None, caption: Any = None, **kw: Any) -> None:
        await self._send_file(_as_file(photo, "image.png"), caption)

    async def send_document(self, chat_id: Any = None, document: Any = None, caption: Any = None, filename: str = "file.txt", **kw: Any) -> None:
        await self._send_file(_as_file(document, filename), caption)

    async def send_chat_action(self, chat_id: Any = None, action: Any = None, **kw: Any) -> None:
        return  # typing indicator is cosmetic; no-op avoids rate churn from keepalive loops


class _DocumentShim:
    """Telegram ``message.document`` shim backed by a discord Attachment."""

    def __init__(self, attachment: discord.Attachment):
        self._a = attachment
        # Discord sets content_type like "application/json; charset=utf-8"; the
        # handler does an exact-membership MIME check, so strip the charset suffix.
        ctype = (attachment.content_type or "").split(";")[0].strip().lower()
        # Some clients send no content-type (or a generic application/octet-stream)
        # for a .json export; rather than let that get rejected by the handler's
        # json/text-only gate, infer json from the filename extension.
        if ctype in ("", "application/octet-stream") and (attachment.filename or "").lower().endswith(".json"):
            ctype = "application/json"
        self.mime_type = ctype or "application/octet-stream"
        self.file_size = attachment.size

    async def get_file(self) -> "_DocumentShim":
        return self

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(await self._a.read())


class _MessageShim:
    """Telegram ``update.message`` shim backed by a discord Message."""

    def __init__(self, ctx: "Ctx", dmsg: discord.Message):
        self._ctx = ctx
        self.text = dmsg.content or ""
        self.document = _DocumentShim(dmsg.attachments[0]) if dmsg.attachments else None

    async def reply_text(self, text: str, parse_mode: Any = None, **kw: Any) -> _Handle:
        return _Handle(await self._ctx.bot._send_text(text))

    async def reply_photo(self, photo: Any, caption: Any = None, **kw: Any) -> None:
        await self._ctx.bot.send_photo(photo=photo, caption=caption)

    async def reply_document(self, document: Any, caption: Any = None, filename: str = "file.txt", **kw: Any) -> None:
        await self._ctx.bot.send_document(document=document, caption=caption, filename=filename)


class _Identified:
    __slots__ = ("id",)

    def __init__(self, _id: int):
        self.id = _id


class Ctx:
    """One object that satisfies BOTH telegram ``update`` and ``context`` so the
    existing handlers run unmodified via ``handler(ctx, ctx)``."""

    def __init__(self, *, bot: Messenger, bot_data: dict, user_data: dict,
                 dmsg: Optional[discord.Message] = None, user_id: int = 0,
                 chat_id: int = 0, args: Optional[list] = None):
        # context.* side
        self.bot = bot
        self.bot_data = bot_data
        self.user_data = user_data
        self.args = args or []
        # update.* side
        self.message = _MessageShim(self, dmsg) if dmsg is not None else None
        self.effective_user = _Identified(user_id)
        self.effective_chat = _Identified(chat_id)


class FakeApp:
    """Stands in for the PTB ``Application`` the background loops consume."""

    def __init__(self, bot: Messenger, bot_data: dict):
        self.bot = bot
        self.bot_data = bot_data
        self._tasks: list[asyncio.Task] = []

    def create_task(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.append(t)
        return t
