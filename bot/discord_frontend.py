"""discord.py front-end for the auto-applier — the Telegram replacement.

Receives messages in the dedicated #applications channel, builds a ``Ctx`` (which
duck-types telegram's update+context), and dispatches to the EXISTING handlers in
``telegram_bot.py`` unchanged. Slash-style ``/command`` text is shlex-parsed into
``ctx.args``; everything else goes to ``handle_text`` (the state machine);
attachments go to ``handle_document``.

Safety mirrors the claude-discord bots: ignore self/other-bots/webhooks/system,
fail-closed single-user allowlist (``JACK_USER_ID``), single-channel scope.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Callable, Optional

import discord

from bot import email_setup
from bot import telegram_bot as tb
from bot.transport import Ctx, FakeApp, Messenger

logger = logging.getLogger("auto-applier-discord")

# Same surface build_app() registered, mapped name → existing handler.
COMMANDS: dict = {
    "start": tb.cmd_start, "help": tb.cmd_help, "status": tb.cmd_status,
    "history": tb.cmd_history, "cancel": tb.cmd_cancel, "search": tb.cmd_search,
    "resume": tb.cmd_resume, "coverletter": tb.cmd_coverletter, "profile": tb.cmd_profile,
    "voice": tb.cmd_voice, "prefs": tb.cmd_prefs, "queue": tb.cmd_queue,
    "report": tb.cmd_report, "failed": tb.cmd_failed, "linkedin": tb.cmd_linkedin,
    "website": tb.cmd_website, "sources": tb.cmd_sources, "captcha": tb.cmd_captcha,
    "handshake": tb.cmd_handshake, "referrals": tb.cmd_referrals, "scams": tb.cmd_scams,
    "scam_apply": tb.cmd_scam_apply, "force": tb.cmd_force,
}

# Commands whose single argument is a raw URL: shlex would split/mangle a pasted
# URL that contains a space or shell metacharacter, so pass the raw remainder.
# (Only /force and /linkedin take a URL; /website takes a theme token, so it must
# stay shlex-parsed like every other command.)
RAW_ARG_COMMANDS = {"force", "linkedin"}


class ApplierDiscord(discord.Client):
    def __init__(self, *, channel_id: int, jack_id: int, bot_data: dict,
                 build_loops: Callable[[FakeApp], None],
                 applicant_ids: Optional[set] = None, env_path: str = ".env"):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._channel_id = channel_id
        self._jack_id = jack_id
        # Scoped applicants (e.g. zvessey) may use ONLY /email to register their own
        # mailbox; everything else (applies, search, force) stays Jack-only.
        self._applicant_ids = set(applicant_ids or set())
        self._env_path = env_path
        self._bot_data = bot_data
        self._build_loops = build_loops
        self._user_store: dict = {}          # user_id -> per-user dict (== context.user_data)
        self._messenger: Optional[Messenger] = None
        self._app: Optional[FakeApp] = None
        self._loops_started = False
        # PTB processes updates sequentially (concurrent_updates=False by default);
        # the handlers + handle_text state machine freely mutate shared user_data/
        # bot_data under that guarantee. discord.py instead dispatches every
        # on_message in its own task, so without this lock two quick messages would
        # race (e.g. double-popping BATCH_QUEUE). Serialize to restore PTB semantics.
        self._dispatch_lock = asyncio.Lock()

    async def on_ready(self) -> None:
        # Loops need the resolved channel; start them ONCE (guard against reconnects).
        # on_ready fires on every (re)connect; the live channel object is reused.
        # Claim the flag BEFORE the first await: discord.py runs each on_ready in its
        # own task, so on a cold-cache first READY (get_channel miss → await
        # fetch_channel yields) a second READY could pass an un-set flag and double-
        # start every poll loop. Setting it first makes the guard atomic.
        if self._loops_started:
            return
        self._loops_started = True
        try:
            channel = self.get_channel(self._channel_id) or await self.fetch_channel(self._channel_id)
            self._messenger = Messenger(channel)
            self._bot_data["authorized_user_id"] = self._jack_id
            app = FakeApp(self._messenger, self._bot_data)
            self._app = app                  # strong ref FIRST, so if _build_loops raises
            self._build_loops(app)           # mid-way the partial task set still has a handle
            logger.info("applier-discord ready as %s in channel %s", self.user, self._channel_id)
        except Exception:
            # If the channel can't be resolved (bad DISCORD_CHANNEL_ID / missing
            # access), the poll loops would never start and the bot would look
            # alive while doing no work. Fail loud and exit so systemd restarts
            # (the unit sets Restart=always, since self.close() exits 0).
            self._messenger = None           # don't leave a half-built messenger live
            self._loops_started = False      # allow a genuine retry on the next READY
            logger.exception("on_ready init failed (channel %s) — closing for restart", self._channel_id)
            await self.close()

    def _ctx(self, message: discord.Message, args: Optional[list] = None) -> Ctx:
        store = self._user_store.setdefault(message.author.id, {})
        return Ctx(
            bot=self._messenger, bot_data=self._bot_data, user_data=store,
            dmsg=message, user_id=message.author.id, chat_id=self._channel_id, args=args,
        )

    async def on_message(self, message: discord.Message) -> None:
        # 1) integrity gate — drop self, other bots, webhook-forged, system messages
        if self.user is not None and message.author.id == self.user.id:
            return
        if message.author.bot or message.webhook_id is not None or message.is_system():
            return
        # 2) single-channel scope
        if getattr(message.channel, "id", None) != self._channel_id:
            return
        # 3) fail-closed allowlist: Jack (full control) or a scoped applicant
        #    (who may only run /email). Everyone else is denied.
        is_jack = message.author.id == self._jack_id
        is_applicant = message.author.id in self._applicant_ids
        if not (is_jack or is_applicant):
            logger.info("deny author=%s", message.author.id)
            return
        if self._messenger is None:  # not ready yet
            return

        # Serialize handler execution (see _dispatch_lock) so the shared state
        # machine sees one message at a time, exactly as PTB delivered them.
        async with self._dispatch_lock:
            content = message.content or ""
            # A scoped applicant (not Jack) may only submit their email.
            if not is_jack:
                head = content[1:].split(None, 1) if content.startswith("/") else []
                if not head or head[0].lower() != "email":
                    await self._messenger._send_text(
                        "You can only register your email here:\n"
                        "`/email <address> <app-password> [imap-host]`"
                    )
                    return
                await self._handle_email(message, head[1] if len(head) > 1 else "")
                return
            try:
                if content.startswith("/"):
                    head = content[1:].split(None, 1)
                    if not head:
                        return
                    name = head[0].lower()
                    remainder = head[1] if len(head) > 1 else ""
                    if name == "email":
                        await self._handle_email(message, remainder)
                        return
                    handler = COMMANDS.get(name)
                    if handler is None:
                        await self._messenger._send_text(f"Unknown command /{name}. Try /help.")
                        return
                    if name in RAW_ARG_COMMANDS:
                        args = [remainder.strip()] if remainder.strip() else []
                    else:
                        try:
                            args = shlex.split(remainder)
                        except ValueError:
                            args = remainder.split()
                    ctx = self._ctx(message, args=args)
                    await handler(ctx, ctx)
                elif message.attachments:
                    if len(message.attachments) > 1:
                        await self._messenger._send_text(
                            f"⚠️ Got {len(message.attachments)} attachments — only the first is processed."
                        )
                    ctx = self._ctx(message)
                    await tb.handle_document(ctx, ctx)
                else:
                    ctx = self._ctx(message)
                    await tb.handle_text(ctx, ctx)
            except Exception:
                logger.exception("handler error")
                try:
                    await self._messenger._send_text("⚠️ internal error (see logs). Send /cancel to reset.")
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _delete_note(deleted: bool) -> str:
        if deleted:
            return "\n🗑️ Your message was deleted so the password isn't left in chat."
        return (
            "\n⚠️ I could NOT delete your message — it still shows your password. "
            "Delete it yourself now and rotate that app password."
        )

    async def _handle_email(self, message: discord.Message, remainder: str) -> None:
        """Register/update the applicant mailbox. Deletes the invoking message FIRST
        (it carries the password); reports the real deletion outcome and never logs
        the secret."""
        # Wipe the password from the channel before anything that could fail. Track
        # the real result so the reply never falsely claims it was deleted.
        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException:
            logger.info("could not delete /email message %s (missing Manage Messages?)", message.id)
        note = self._delete_note(deleted)

        address, password, host = email_setup.parse_email_command(remainder)
        if not address or not password:
            await self._messenger._send_text(
                "Usage: `/email <address> <app-password> [imap-host]`" + note)
            return
        try:
            # Update BOTH the bot's profile.yaml and the apply workspace's
            # (/opt/auto-applier) — the apply subprocess reads its own copy for the
            # email it types onto forms.
            from bot.mcp_apply import MCP_DIR
            profile_paths = [
                self._bot_data.get("profile_path", "profile.yaml"),
                os.path.join(MCP_DIR, "profile.yaml"),
            ]
            summary = await email_setup.submit_email(
                address, password,
                env_path=self._env_path,
                profile_paths=profile_paths,
                explicit_host=host,
            )
        except email_setup.EmailSetupError as exc:
            await self._messenger._send_text(f"⚠️ {exc}{note}")
            return
        except Exception:  # noqa: BLE001 — never surface a trace that might hold the secret
            logger.exception("email setup failed (no secret logged)")
            await self._messenger._send_text("⚠️ Internal error saving the email (see logs)." + note)
            return
        await self._messenger._send_text(summary + note)
