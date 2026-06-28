"""Find Bay Area job application URLs for the candidate, via claude -p + Playwright.

Searches BOTH Google (for ATS / company-careers postings) AND LinkedIn Jobs, then
returns direct application URLs. The apply step (mcp_apply.apply_via_mcp) applies to
each as the candidate in profile.yaml. Same subprocess pattern as mcp_apply: shells
out to `claude -p` with the Playwright MCP in the proven /opt/auto-applier workspace.

Strictly a DISCOVERY step — it reads job boards, never the candidate's email.
"""
import asyncio
import json
import logging
import os
import re
import time

MCP_DIR = "/opt/auto-applier"
logger = logging.getLogger("auto-applier-discord")

_FINDER_PROMPT = """Find live job-application URLs for the candidate described in profile.yaml.
Read profile.yaml first for their target roles, skills and seniority. {query_line}

HARD LOCATION RULE: San Francisco BAY AREA only (SF, East Bay, Peninsula, South Bay/Silicon
Valley, North Bay/Marin). Ignore anything outside the Bay Area. Remote-only with no Bay Area
anchor does NOT count.

Use the playwright MCP browser tools (mcp__playwright__*). Do BOTH of these:

1) WEB SEARCH (Google; if it shows a CAPTCHA, use Bing or DuckDuckGo instead): run a few query
   variants combining the roles with Bay Area locations and the common ATS hosts, e.g.
     <role> ("San Francisco" OR "Bay Area" OR Oakland OR "Palo Alto" OR "San Jose") site:boards.greenhouse.io
     <role> ("San Francisco" OR "Bay Area") site:jobs.lever.co
     <role> ("San Francisco" OR "Bay Area") site:jobs.ashbyhq.com
   Open promising results and capture the DIRECT application URL (the page with the apply form).

2) LINKEDIN JOBS: go to https://www.linkedin.com/jobs/search and search the roles with the
   location set to "San Francisco Bay Area". For each relevant posting, capture its application
   URL — if it links out to an external ATS (greenhouse/lever/ashby/workday/etc.), use that
   external URL; otherwise use the LinkedIn job URL. If LinkedIn requires login and you can't
   proceed, just rely on the web-search results.

Collect only DIRECT application URLs for Bay Area roles that fit the candidate. De-duplicate.
Aim for up to {n} URLs.

Output the FINAL result on the LAST line as compact JSON, EXACTLY in this form (nothing after it):
RESULT_URLS: ["https://...", "https://..."]
If you find none, output exactly: RESULT_URLS: []"""


async def find_jobs(query: str = "", max_results: int = 25, timeout: int = 1200) -> list[str]:
    """Return a list of Bay Area application URLs. `query` is optional extra role/keyword
    guidance; when empty the agent uses the candidate's desired_roles from profile.yaml."""
    query_line = (
        f"Focus the search on: {query}." if query.strip()
        else "Use the candidate's desired_roles from profile.yaml as the search terms."
    )
    prompt = _FINDER_PROMPT.format(query_line=query_line, n=max_results)

    env = dict(os.environ)
    env["HOME"] = "/home/claude"
    env["IS_SANDBOX"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    # The claude -p subprocess authenticates with CLAUDE_CODE_OAUTH_TOKEN from the
    # bot's env (the /home/claude stored creds are stale and 401). Warn loudly if
    # it's missing so a silent empty result isn't mistaken for "no jobs".
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.error("job_finder: CLAUDE_CODE_OAUTH_TOKEN not in env — claude -p will 401")
    cmd = [
        "claude", "-p", "--output-format", "text",
        "--mcp-config", os.path.join(MCP_DIR, ".mcp.json"), "--strict-mcp-config",
        prompt,
    ]
    logger.info("job_finder: launching search (query=%r, max=%d, timeout=%ds)",
                query or "<profile desired_roles>", max_results, timeout)
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
        logger.warning("job_finder: search TIMED OUT after %ds — killed", timeout)
        return []

    elapsed = int(time.monotonic() - t0)
    text = out.decode("utf-8", errors="replace") if out else ""
    urls = _parse_urls(text, max_results)
    logger.info("job_finder: rc=%s, %d urls, %ds", proc.returncode, len(urls), elapsed)
    if not urls:
        # Surface WHY (auth error, captcha, refusal, empty) instead of a silent zero.
        tail = text[-1000:].replace("\n", " | ").strip()
        logger.warning("job_finder: 0 urls (rc=%s) — output tail: %s", proc.returncode, tail or "<empty>")
    return urls


def _parse_urls(text: str, max_results: int) -> list[str]:
    """Pull the RESULT_URLS JSON list from the agent output. Falls back to scraping any
    http(s) URLs if the sentinel is malformed."""
    seen: list[str] = []
    m = None
    for m in re.finditer(r"RESULT_URLS:\s*(\[.*?\])", text, re.S):
        pass  # keep the LAST match
    if m:
        try:
            for u in json.loads(m.group(1)):
                if isinstance(u, str) and u.startswith("http") and u not in seen:
                    seen.append(u)
        except (ValueError, TypeError):
            pass
    if not seen:  # sentinel missing/garbled — best-effort scrape
        for u in re.findall(r"https?://[^\s\"'\]\),]+", text):
            if u not in seen:
                seen.append(u)
    return seen[:max_results]
