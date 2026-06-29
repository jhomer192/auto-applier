"""Conversational Discord front-end for the auto-applier.

This replaces the old Python state machine (discord_frontend + telegram_bot) with
the SAME pattern the classistant/coder bots use: Claude IS the bot, via the Claude
Agent SDK, and a small set of in-process MCP tools (bot/job_tools.py) are its
hands. So you just talk to it — "find bay area SOC jobs and apply", "did you apply
to all of them?", "my email is x, app password y" — and it answers or acts.

Scope + safety:
  * One channel (#applications); fail-closed allowlist = Jack + scoped applicants.
  * The apply engine (mcp_apply.apply_via_mcp) is unchanged and still enforces
    Bay-Area-only and applies ONLY as the candidate in profile.yaml.
  * No email-send path exists anywhere; the only mailbox access is the apply-time
    PIN reader. A message that carries an app password is deleted from the channel
    BEFORE anything else and never enters the model's context.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

import discord
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from bot import email_setup
from bot.job_tools import build_job_tools

logger = logging.getLogger("auto-applier-discord")

WORKING = "⏳ working…"
LONG_OUTPUT_CHARS = 6000
INIT_RETRIES = 2     # transient "Control request timeout: initialize" → rebuild + retry
QUERY_TIMEOUT = 300  # hard cap on a single Claude turn so a wedged CLI can't lock the channel

_SYSTEM_PROMPT = """You are the job-application assistant in Jack's private Discord, working on
behalf of ONE candidate — the person described in profile.yaml (read it when you need their
details). You are NOT a command bot: talk like a normal, concise person and use your tools to
get things done.

THE GOAL: land this candidate ANY entry-level Bay-Area job. His background is cybersecurity, but
he's equally open to sales/BDR, operations, customer support/success, IT support, and other
intro-level roles — breadth wins, don't hold out for a perfect-fit title (and he is not a
software engineer, so skip pure SWE/coding roles). When asked to "find jobs" with no specifics,
cast a wide net across these lanes.

Your tools:
- find_jobs(query): list current OPEN Bay-Area roles that fit the candidate (does not apply).
- apply_jobs(urls/query): apply on the candidate's behalf in the background; each result is
  posted to this channel as it lands. Use this when asked to apply.
- application_status(): what you've actually applied to. Use this to ANSWER questions like
  "did you apply to all of them?" or "why did you stop?" — never start a new search to answer
  a question.
- set_email(address, app_password): register the candidate's application email.

How to behave:
- When the user ASKS something, answer it (use application_status for status questions). Only
  search or apply when they actually ask you to.
- When they say something like "find and apply to bay area security jobs", call find_jobs then
  apply_jobs (or apply_jobs with a query) and tell them it's running.
- Applying happens in the background and can take several minutes per job; don't wait on it —
  kick it off, say so, and let the per-job result lines speak for themselves.
- HARD RULES (also enforced by the tools, so don't try to work around them): only San Francisco
  Bay-Area jobs, ever; only apply as the candidate in profile.yaml, never anyone else.
- If a batch pauses because the Claude usage limit was hit, say so plainly and offer to continue
  later — do not pretend the remaining jobs failed. To RESUME a paused batch, call apply_jobs with
  no arguments (it picks up exactly where it left off).
Be brief. No filler."""


def _looks_like_email(tok: str) -> bool:
    return "@" in tok and "." in tok.rsplit("@", 1)[-1]


def _extract_app_password(tokens: list[str]) -> str:
    """Return a Gmail app-password if these tokens contain one, else ''.

    Gmail app passwords are 16 lowercase letters, shown as one block or as four
    4-letter groups. Requiring that exact shape (not just 'some letters') is what
    keeps ordinary prose that happens to mention an address from being treated as
    a credential."""
    alpha = [t for t in tokens if t.isalpha()]
    for t in alpha:
        if len(t) == 16:
            return t.lower()
    # any window of 4 consecutive 4-letter alpha tokens
    for i in range(len(tokens) - 3):
        window = tokens[i:i + 4]
        if all(w.isalpha() and len(w) == 4 for w in window):
            return "".join(window).lower()
    return ""


def find_credential(content: str):
    """If *content* is an email registration (an explicit /email command, or an
    address accompanied by an app-password-shaped secret), return (address,
    password); otherwise None so the message goes to the model normally.

    The point of catching it pre-model is to DELETE the secret from the channel
    before it can be seen/stored — so detection is position-independent but must
    see a real secret (or an explicit /email intent)."""
    explicit = content.lower().startswith("/email")
    body = content[len("/email"):].strip() if explicit else content
    tokens = body.split()
    address = next((t.strip(".,;") for t in tokens if _looks_like_email(t)), "")
    password = _extract_app_password([t for t in tokens if not _looks_like_email(t)])
    if explicit:
        return (address, password)          # explicit registration (password optional)
    if address and password:
        return (address, password)          # a pasted credential to protect
    return None


def _chunks(text: str, limit: int = 2000) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(line) > limit:
            if cur:
                out.append(cur); cur = ""
            for i in range(0, len(line), limit):
                out.append(line[i:i + limit])
            continue
        cand = line if not cur else cur + "\n" + line
        if len(cand) > limit:
            out.append(cur); cur = line
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out or [""]


class ApplierAgent(discord.Client):
    def __init__(self, *, channel_id: int, jack_id: int, bot_data: dict,
                 applicant_ids: set | None = None, env_path: str = ".env"):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._channel_id = channel_id
        self._jack_id = jack_id
        self._applicant_ids = set(applicant_ids or set())
        self._env_path = os.path.abspath(env_path)
        self.db = bot_data.get("db")
        self.profile = bot_data.get("profile") or {}
        self.candidate_name = (self.profile.get("name") if isinstance(self.profile, dict) else "") or ""
        self._profile_path = bot_data.get("profile_path", "profile.yaml")

        self._channel: discord.abc.Messageable | None = None
        self._session: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()          # one in-flight Claude query at a time
        self._mcp_server = None
        self._mcp_names: list[str] = []

        # background apply-batch state (read by job_tools)
        self.batch_running = False
        self.batch_done = 0
        self.paused_remaining: list[str] = []
        self.last_found: list[str] = []
        self._batch_task: asyncio.Task | None = None

    # ---- lifecycle ----------------------------------------------------------

    async def on_ready(self) -> None:
        if self._channel is not None:
            return
        try:
            self._channel = self.get_channel(self._channel_id) or await self.fetch_channel(self._channel_id)
        except Exception:
            logger.exception("could not resolve channel %s — closing for restart", self._channel_id)
            await self.close()
            return
        if self._mcp_server is None:
            self._mcp_server, self._mcp_names = build_job_tools(self)
        logger.info("applier-agent ready as %s in channel %s (candidate=%r, tools=%d)",
                    self.user, self._channel_id, self.candidate_name, len(self._mcp_names))

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        await super().close()

    def _make_options(self) -> ClaudeAgentOptions:
        opts: dict = {
            "cwd": os.getcwd(),
            "permission_mode": "bypassPermissions",
            "system_prompt": _SYSTEM_PROMPT,
        }
        if self._mcp_server is not None:
            opts["mcp_servers"] = {"jobs": self._mcp_server}
            opts["allowed_tools"] = list(self._mcp_names)
        return ClaudeAgentOptions(**opts)

    async def _ensure_session(self) -> ClaudeSDKClient:
        if self._session is None:
            client = ClaudeSDKClient(options=self._make_options())
            await client.__aenter__()
            self._session = client
        return self._session

    async def _drop_session(self) -> None:
        sess, self._session = self._session, None
        if sess is not None:
            try:
                await sess.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    async def _ask_claude(self, prompt: str) -> str:
        # Serialize queries (the SDK session holds the running conversation). Each
        # attempt is bounded by QUERY_TIMEOUT so a hung claude CLI (never emits a
        # ResultMessage) can't hold the lock — and thus the whole channel — forever.
        # We only retry when the attempt failed BEFORE Claude produced anything, so a
        # mid-stream failure never replays a side-effecting prompt (apply/set_email)
        # onto a fresh, context-less session.
        async with self._lock:
            last_exc: Exception | None = None
            for attempt in range(1, INIT_RETRIES + 1):
                produced = False
                client = await self._ensure_session()

                async def _run() -> str:
                    nonlocal produced
                    await client.query(prompt)
                    parts: list[str] = []
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            produced = True  # Claude started responding/acting
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    parts.append(block.text)
                    return "".join(parts).strip()

                try:
                    return await asyncio.wait_for(_run(), timeout=QUERY_TIMEOUT)
                except Exception as exc:  # noqa: BLE001 (incl. TimeoutError)
                    last_exc = exc
                    logger.warning("claude query attempt %d/%d failed: %s",
                                   attempt, INIT_RETRIES, type(exc).__name__)
                    await self._drop_session()
                    if produced:
                        break  # don't replay a prompt Claude already started acting on
            raise last_exc if last_exc else RuntimeError("query failed")

    # ---- message handling ---------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        if message.author.bot or message.webhook_id is not None or message.is_system():
            return
        if getattr(message.channel, "id", None) != self._channel_id:
            return
        is_jack = message.author.id == self._jack_id
        if not (is_jack or message.author.id in self._applicant_ids):
            logger.info("deny author=%s", message.author.id)
            return
        if self._channel is None:
            return

        content = (message.content or "").strip()
        if not content:
            return

        # Credential safety: if the message is an email registration carrying an app
        # password (anywhere in it) or an explicit /email command, delete it FIRST so
        # the secret leaves the channel, handle it directly, and never feed it to the
        # model. Ordinary chat that merely mentions an address still reaches Claude.
        cred = find_credential(content)
        if cred is not None:
            await self._handle_email_message(message, cred[0], cred[1])
            return

        await self._engage(message, content)

    async def _engage(self, message: discord.Message, body: str) -> None:
        asker = "Jack" if message.author.id == self._jack_id else getattr(
            message.author, "display_name", "the applicant")
        working: discord.Message | None = None
        try:
            async with message.channel.typing():
                working = await message.reply(WORKING, mention_author=False)
                reply = await self._ask_claude(f"[{asker}]: {body}")
        except Exception as exc:  # noqa: BLE001 — safe error, never a stack trace/secret
            logger.exception("claude query failed")
            err = f"⚠️ error: {type(exc).__name__}"
            if working is not None:
                await working.edit(content=err)
            else:
                await message.reply(err, mention_author=False)
            return
        await self._deliver(message, working, reply)

    async def _deliver(self, message: discord.Message, working: discord.Message | None, reply: str) -> None:
        reply = (reply or "").strip()
        if not reply:
            # Claude chose to stay silent (e.g. nothing to add) — honor that: drop the
            # placeholder and post nothing, rather than emitting "(no output)" noise.
            if working is not None:
                try:
                    await working.delete()
                except discord.HTTPException:
                    pass
            return
        if len(reply) > LONG_OUTPUT_CHARS:
            if working is not None:
                await working.edit(content="📄 full reply attached:")
            await message.channel.send(file=discord.File(
                io.BytesIO(reply.encode("utf-8")), filename="reply.txt"))
            return
        parts = _chunks(reply, 2000)
        if working is not None:
            await working.edit(content=parts[0])
        else:
            await message.channel.send(parts[0])
        for chunk in parts[1:]:
            await message.channel.send(chunk)

    # ---- channel posting (used by the background batch) ---------------------

    async def post(self, text: str) -> None:
        if self._channel is None:
            return
        try:
            for part in _chunks(text, 2000):
                await self._channel.send(part)
        except discord.HTTPException:
            logger.warning("post to channel failed")

    # ---- background apply batch --------------------------------------------

    def start_batch(self, urls: list[str]) -> bool:
        """Launch a background apply batch. Returns False if one is already running,
        so the caller never claims a batch started when it didn't."""
        if self.batch_running:
            return False
        self.batch_running = True
        self.batch_done = 0
        self.paused_remaining = []  # a fresh batch supersedes any abandoned pause
        self._batch_task = asyncio.create_task(self._run_batch(list(urls)))
        return True

    async def _run_batch(self, urls: list[str]) -> None:
        from bot.mcp_apply import apply_via_mcp
        applied = blocked = failed = 0
        try:
            for i, url in enumerate(urls):
                try:
                    res = await apply_via_mcp(url)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await self.post(f"❌ error on {url}: {type(exc).__name__}")
                    continue
                result = (res.get("result") or "").strip()
                detail = (res.get("detail") or "").strip()

                if result == "USAGE_LIMIT":
                    # Stop honestly and remember the rest so "continue" can resume.
                    self.paused_remaining = urls[i:]
                    await self.post(
                        f"⏸ Paused — Claude usage limit hit{(' (' + detail + ')') if detail else ''}. "
                        f"{len(self.paused_remaining)} job(s) left. Say 'continue' when it resets and "
                        "I'll pick up where I left off.")
                    return

                await self._record_apply(url, res)
                if res.get("success"):
                    applied += 1; emoji = "✅"
                elif result.lower().startswith("blocked"):
                    blocked += 1; emoji = "⛔"
                else:
                    failed += 1; emoji = "❌"
                self.batch_done += 1
                line = f"{result} {detail}".strip() or "done"
                await self.post(f"{emoji} {line}\n{url}")
            await self.post(
                f"🏁 Done — {applied} applied, {blocked} skipped (not Bay Area / closed), {failed} failed.")
        except Exception:
            logger.exception("apply batch crashed")
            await self.post("⚠️ The apply batch hit an internal error (see logs).")
        finally:
            self.batch_running = False

    async def _record_apply(self, url: str, res: dict) -> None:
        if not self.db:
            return
        from bot.models import ApplicationRecord
        result = (res.get("result") or "").strip()
        detail = (res.get("detail") or "").strip()
        status = "applied" if res.get("success") else (
            "skipped" if result.lower().startswith("blocked") else "failed")
        try:
            await self.db.insert_application(ApplicationRecord(
                url=url, title="", company="", site="mcp", status=status,
                applied_at=datetime.now(timezone.utc).isoformat() if status == "applied" else None,
                notes=f"{result} {detail}".strip()[:500],
            ))
        except Exception:  # noqa: BLE001
            logger.exception("could not record apply for %s", url)

    # ---- email registration -------------------------------------------------

    def _profile_paths(self) -> list[str]:
        from bot.mcp_apply import MCP_DIR
        return [self._profile_path, os.path.join(MCP_DIR, "profile.yaml")]

    async def register_email(self, address: str, app_password: str) -> str:
        """Set the application email (and, with an app password, verify the mailbox
        and enable PIN auto-read). Returns a user-facing summary; never logs a secret."""
        paths = self._profile_paths()
        if not app_password:
            email_setup.set_form_email(paths, address)
            return (f"✅ Saved **{address}** as the application email. No app password given, so I "
                    "can't auto-read verification PINs yet — send one to turn that on.")
        try:
            return await email_setup.submit_email(
                address, app_password, env_path=self._env_path,
                profile_paths=paths, explicit_host=None)
        except email_setup.EmailSetupError as exc:
            email_setup.set_form_email(paths, address)
            return (f"✅ Saved **{address}** for applications, but I couldn't verify the inbox: {exc}\n"
                    "⚠️ Verification-PIN auto-read is OFF until you send a working app password.")

    async def _handle_email_message(self, message: discord.Message, content: str) -> None:
        # Wipe the password from the channel before anything that could fail.
        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException:
            logger.info("could not delete email message %s (missing Manage Messages?)", message.id)
        note = ("\n🗑️ Your message was deleted so the password isn't left in chat." if deleted else
                "\n⚠️ I could NOT delete your message — it still shows your password. Delete it now "
                "and rotate that app password.")
        address, password, _host = email_setup.parse_email_command(content)
        if not address:
            await self.post("Send your email like:  `you@gmail.com your-app-password`" + note)
            return
        try:
            summary = await self.register_email(address, password)
        except Exception:  # noqa: BLE001 — never surface a trace that might hold the secret
            logger.exception("register_email failed (no secret logged)")
            await self.post("⚠️ Internal error saving the email (see logs)." + note)
            return
        await self.post(summary + note)
