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

_FINDER_PROMPT = """Find live Bay-Area job-application URLs for the candidate in profile.yaml
(read it first for their target roles/skills). {query_line}

Work FAST — you have limited time, so do NOT open each result page; just harvest URLs from
search-result pages. Use the playwright MCP browser tools.

PRIMARY — DuckDuckGo HTML (no JavaScript, no CAPTCHA — returns a plain list of links). For each
query, navigate to:  https://html.duckduckgo.com/html/?q=<url-encoded query>  and read the result
links from the page snapshot. Run these queries, substituting the candidate's roles for <role>:
  1. <role> San Francisco Bay Area site:boards.greenhouse.io
  2. <role> San Francisco Bay Area site:jobs.lever.co
  3. <role> Bay Area site:jobs.ashbyhq.com
  4. <role> "San Francisco" OR "Bay Area" jobs apply
From the results, COLLECT job-posting URLs — especially boards.greenhouse.io/*/jobs/*,
jobs.lever.co/*, jobs.ashbyhq.com/*, and company careers/apply pages. Keep only roles that plausibly
fit the candidate and are in the Bay Area. De-duplicate.

IF DuckDuckGo returns NO results or shows a challenge/blank page, do NOT give up — immediately run
the SAME queries on Bing instead:  https://www.bing.com/search?q=<url-encoded query>  and harvest the
result links there. Try at least two engines before concluding there are no jobs.

THEN (only if time remains) try LinkedIn once:
  https://www.linkedin.com/jobs/search?keywords=<role>&location=San%20Francisco%20Bay%20Area
Grab any external ATS apply URLs visible without logging in. If it demands a login, SKIP it.

Aim for up to {n} URLs total. Spend no more than a few minutes.

Output the FINAL result on the LAST line as compact JSON, EXACTLY (nothing after it):
RESULT_URLS: ["https://...", "https://..."]
If you find none, output exactly: RESULT_URLS: []"""


async def find_jobs(query: str = "", max_results: int = 25, attempts: int = 2) -> list[str]:
    """Return Bay Area application URLs.

    PRIMARY source is the live ATS board APIs (bot.job_boards) — current, OPEN roles
    returned as JSON in seconds, so the applier lands on live forms instead of the
    stale/closed postings a search engine indexes. Only when the boards come up short
    do we supplement with the search-engine agent (which is slower and flakier)."""
    from bot import job_boards
    board: list[str] = []
    try:
        board = await job_boards.find_board_jobs(max_results, query)
    except Exception:  # noqa: BLE001
        logger.exception("job_finder: live board lookup failed")
    if len(board) >= max_results:
        logger.info("job_finder: %d urls from live boards (skipping agent search)", len(board))
        return board[:max_results]

    # Boards thin → supplement with the search-engine agent (retry on empty).
    query_line = (
        f"Focus the search on: {query}." if query.strip()
        else "Use the candidate's desired_roles from profile.yaml as the search terms."
    )
    prompt = _FINDER_PROMPT.format(query_line=query_line, n=max_results)
    agent: list[str] = []
    for attempt in range(1, attempts + 1):
        agent = await _search_once(prompt, query, max_results)
        if agent:
            break
        if attempt < attempts:
            logger.warning("job_finder: agent attempt %d/%d found 0 — retrying", attempt, attempts)
    seen = set(board)
    merged = board + [u for u in agent if not (u in seen or seen.add(u))]
    logger.info("job_finder: %d urls (%d live-board + %d agent)",
                len(merged), len(board), len(merged) - len(board))
    return merged[:max_results]


async def _search_once(prompt: str, query: str, max_results: int) -> list[str]:
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
    logger.info("job_finder: launching search (query=%r, max=%d)",
                query or "<profile desired_roles>", max_results)
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=MCP_DIR, env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()

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
