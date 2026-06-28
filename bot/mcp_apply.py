"""Model-driven apply bridge.

The May-13 hardcoded ATS adapters can't read modern Greenhouse/Lever forms
(they scrape 0 fields). This module replaces them: it shells out to
`claude -p` with the Playwright MCP — the same engine that applied to xAI and
Machinify end-to-end (real confirmation emails, PINs read from the inbox).

It runs in the proven /opt/auto-applier workspace, which has the playwright MCP
config (.mcp.json), the permission allowlist (.claude/settings.json),
profile.yaml, data/resume.pdf, and scripts/check_email.cjs already wired.
"""
import asyncio
import logging
import os
import re
import time

from bot.bay_area import BAY_AREA_RULE

MCP_DIR = "/opt/auto-applier"
logger = logging.getLogger("auto-applier-discord")

_PROMPT = """Apply to this job on behalf of the candidate described in profile.yaml (read it
FIRST and use that person's name and details throughout — do not assume any other identity).
Apply end to end, autonomously. URL: {url}

Use the playwright MCP browser tools (mcp__playwright__*) and the repo files in this directory.

{bay_area_rule}

Steps:
1. Open the URL. If it redirects to an error/closed page, report RESULT: BLOCKED job-closed.
2. LOCATION CHECK (mandatory, before filling anything): apply the LOCATION RESTRICTION above.
   If the role is not in the Bay Area, report RESULT: BLOCKED not-bay-area and stop — do not fill or submit.
3. Read profile.yaml for Jack's details.
4. Fill ALL fields from profile.yaml: name, email, phone, location, current company and title
   (from work_history), education, certifications, links, and strong TRUTHFUL answers to any
   essay/custom questions drawn from their summary/experience. Use ONLY facts present in profile.yaml.
5. Handle dropdowns by clicking and selecting the option by visible text. Visa sponsorship = No.
   Demographics/EEO = Decline to self-identify. Never lie on any field.
6. Attach the resume: upload data/resume.pdf to the Resume/CV field.
7. Click Submit.
8. If a verification code / security PIN page appears, get the code with:
   node scripts/check_email.cjs --code --since 10m  (retry every 20s up to 8 minutes), enter it, confirm.
9. Verify success — a confirmation page / "thank you for applying". Only then is it applied.
Report the FINAL outcome on the LAST line, EXACTLY one of:
RESULT: APPLIED
RESULT: BLOCKED <short reason>
RESULT: FAILED <short reason>"""


async def apply_via_mcp(url: str, timeout: int = 900) -> dict:
    """Drive a full application via claude -p + Playwright MCP. Returns
    {success, result, detail, raw}."""
    env = dict(os.environ)
    env["HOME"] = "/home/claude"          # workspace for the claude user
    env["IS_SANDBOX"] = "1"               # allow --dangerously-skip-permissions as root
    env.pop("ANTHROPIC_API_KEY", None)    # force OAuth subscription, not API key
    # Auth is via CLAUDE_CODE_OAUTH_TOKEN from the bot env (the /home/claude stored
    # creds are stale and 401). Warn loudly so a silent failure isn't misread.
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.error("apply_via_mcp: CLAUDE_CODE_OAUTH_TOKEN not in env — claude -p will 401")
    cmd = [
        "claude", "-p", "--output-format", "text",
        "--mcp-config", os.path.join(MCP_DIR, ".mcp.json"), "--strict-mcp-config",
        _PROMPT.format(url=url, bay_area_rule=BAY_AREA_RULE),
    ]
    logger.info("apply_via_mcp: starting apply for %s", url)
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=MCP_DIR, env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning("apply_via_mcp: TIMED OUT after %ds for %s", timeout, url)
        return {"success": False, "result": "TIMEOUT", "detail": "", "raw": ""}

    elapsed = int(time.monotonic() - t0)
    text = (out or b"").decode("utf-8", "replace")
    m = re.search(r"RESULT:\s*(APPLIED|BLOCKED|FAILED)\b(.*)$", text, re.MULTILINE)
    if m:
        status = m.group(1)
        detail = (m.group(2) or "").strip()
    else:
        low = text.lower()
        status = "APPLIED" if ("thank you for applying" in low or "application submitted" in low) else "UNKNOWN"
        detail = ""
    logger.info("apply_via_mcp: rc=%s result=%s %s (%ds) %s",
                proc.returncode, status, detail, elapsed, url)
    if status in ("UNKNOWN", "FAILED"):
        tail = text[-800:].replace("\n", " | ").strip()
        logger.warning("apply_via_mcp: %s for %s — output tail: %s", status, url, tail or "<empty>")
    return {"success": status == "APPLIED", "result": status, "detail": detail, "raw": text[-600:]}
