"""In-process MCP tools that give the applier-bot's Claude its "extra tools".

These run on the SAME asyncio event loop as discord.py (the Claude Agent SDK
awaits in-process tool handlers inline), so a handler can await discord.py and
the existing async job modules directly.

`build_job_tools(bot)` closes over the live ApplierAgent instance. The bot is the
brain; these four tools are the hands:
  - find_jobs           : list current OPEN Bay-Area roles (does NOT apply)
  - apply_jobs          : apply on the candidate's behalf in the BACKGROUND,
                          posting each result to the channel as it lands
  - application_status  : what we've applied to (so "did you apply to all?" is
                          answered from real data, never a new search)
  - set_email           : register the candidate's mailbox for PIN auto-read

Safety invariants live INSIDE the tools, not the prompt (so they can't be
talked around): every apply goes through mcp_apply.apply_via_mcp, which enforces
Bay-Area-only and applies ONLY as the candidate in profile.yaml. There is no
email-send capability anywhere.
"""
from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import create_sdk_mcp_server, tool

logger = logging.getLogger("auto-applier-discord")


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {text}"}], "is_error": True}


def build_job_tools(bot):
    """Return (mcp_server, [tool_name, ...]) wired to the live ApplierAgent."""

    async def _fresh_urls(query: str, max_results: int) -> list[str]:
        """Find open Bay-Area URLs and drop ones already applied to."""
        from bot import job_finder
        urls = await job_finder.find_jobs(query, max_results=max_results)
        db = bot.db
        fresh: list[str] = []
        for u in urls:
            try:
                if db and await db.is_already_applied(u):
                    continue
            except Exception:  # noqa: BLE001
                pass
            fresh.append(u)
        return fresh

    @tool(
        "find_jobs",
        "Search live company job boards for CURRENTLY-OPEN Bay-Area roles that fit the "
        "candidate, and return the application URLs. This only lists jobs; it does NOT "
        "apply. Optionally pass a query to focus the roles (e.g. 'SOC analyst, GRC').",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "optional role focus; blank = the candidate's default target roles"},
                "max_results": {"type": "integer", "description": "max URLs to return (default 25)"},
            },
        },
    )
    async def find_jobs(args):
        query = (args.get("query") or "").strip()
        try:
            n = int(args.get("max_results") or 25)
        except (TypeError, ValueError):
            n = 25
        try:
            fresh = await _fresh_urls(query, n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("find_jobs tool failed")
            return _err(f"search failed: {type(exc).__name__}")
        bot.last_found = fresh
        if not fresh:
            return _ok("No new open Bay-Area roles found this pass (everything matched was "
                       "already applied to or closed). Try different role keywords.")
        listing = "\n".join(f"- {u}" for u in fresh)
        return _ok(f"Found {len(fresh)} open Bay-Area role(s) not yet applied to:\n{listing}\n\n"
                   "Call apply_jobs to apply to these on the candidate's behalf.")

    @tool(
        "apply_jobs",
        "Apply to jobs on the candidate's behalf, IN THE BACKGROUND. Each result is posted "
        "to this channel as it completes (✅ applied / ⛔ skipped / ❌ failed). Pass `urls` to "
        "apply to specific links, OR a `query` to find Bay-Area roles and apply to them, OR "
        "neither to apply to the most recent find_jobs results (and resume after a usage-limit "
        "pause). Returns immediately; it does not block. Only ONE batch runs at a time.",
        {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "specific application URLs to apply to"},
                "query": {"type": "string", "description": "role focus to find+apply (used only when urls is empty)"},
                "max_results": {"type": "integer", "description": "max jobs to find when using query (default 25)"},
            },
        },
    )
    async def apply_jobs(args):
        if bot.batch_running:
            done = bot.batch_done
            return _ok(f"Already applying ({done} done so far) — I'll keep going and post each "
                       "result here. No need to start another batch.")
        urls = [u for u in (args.get("urls") or []) if isinstance(u, str) and u.startswith("http")]
        query = (args.get("query") or "").strip()
        try:
            n = int(args.get("max_results") or 25)
        except (TypeError, ValueError):
            n = 25

        if not urls:
            if query:
                try:
                    urls = await _fresh_urls(query, n)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("apply_jobs find failed")
                    return _err(f"search failed: {type(exc).__name__}")
            elif bot.paused_remaining:
                urls = bot.paused_remaining
                bot.paused_remaining = []
            else:
                urls = list(getattr(bot, "last_found", []) or [])

        if not urls:
            return _ok("Nothing to apply to — run find_jobs first, or give me a role to search for.")

        bot.start_batch(urls)
        who = bot.candidate_name or "the candidate"
        return _ok(f"Started applying to {len(urls)} Bay-Area role(s) as {who}. I'll post each "
                   "result here as it completes, and tell you when the batch is done.")

    @tool(
        "application_status",
        "Report what the applier has actually done: totals by status (applied / skipped / "
        "failed) and the most recent applications from the database. Use THIS to answer "
        "questions like 'did you apply to all of them?' or 'what have you applied to?' — never "
        "start a new search to answer a question.",
        {"type": "object", "properties": {"limit": {"type": "integer", "description": "how many recent rows to list (default 15)"}}},
    )
    async def application_status(args):
        try:
            limit = int(args.get("limit") or 15)
        except (TypeError, ValueError):
            limit = 15
        db = bot.db
        if not db:
            return _err("no database available")
        try:
            stats = await db.get_stats()
            recent = await db.get_recent(limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("application_status failed")
            return _err(f"could not read history: {type(exc).__name__}")
        total = sum(stats.values())
        head = (f"Totals: {stats.get('applied', 0)} applied, {stats.get('skipped', 0)} skipped, "
                f"{stats.get('failed', 0)} failed ({total} tracked).")
        running = ""
        if bot.batch_running:
            running = f"\n⏳ A batch is running right now: {bot.batch_done} done so far."
        elif bot.paused_remaining:
            running = f"\n⏸ Paused with {len(bot.paused_remaining)} left (usage limit). Say 'continue' to resume."
        lines = []
        for r in recent:
            note = (r.notes or "").strip()
            lines.append(f"- [{r.status}] {note[:80] or r.url}")
        body = ("\n".join(lines)) if lines else "No applications recorded yet."
        return _ok(f"{head}{running}\n\nRecent:\n{body}")

    @tool(
        "set_email",
        "Register or update the candidate's application email. With an app password it also "
        "enables auto-reading the verification PIN during applications. (The bot only ever "
        "READS that mailbox for codes — it can never send mail.)",
        {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "the email address to use on applications"},
                "app_password": {"type": "string", "description": "optional Gmail app password to enable PIN auto-read"},
            },
            "required": ["address"],
        },
    )
    async def set_email(args):
        address = (args.get("address") or "").strip()
        app_password = (args.get("app_password") or "").strip()
        if "@" not in address or "." not in address:
            return _err("that doesn't look like an email address")
        try:
            summary = await bot.register_email(address, app_password)
        except Exception as exc:  # noqa: BLE001 — never echo a secret in the error
            logger.exception("set_email failed")
            return _err(f"could not save the email: {type(exc).__name__}")
        return _ok(summary)

    handlers = [find_jobs, apply_jobs, application_status, set_email]
    server = create_sdk_mcp_server(name="jobs", version="1.0.0", tools=handlers)
    names = [f"mcp__jobs__{h.name}" for h in handlers]
    return server, names
