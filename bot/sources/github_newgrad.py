"""Scrape community-maintained GitHub new-grad job repos.

Parses markdown tables from three actively-maintained repos:
  - SimplifyJobs/New-Grad-Positions  (the canonical list, updated daily)
  - speedyapply/2026-SWE-College-Jobs (community-curated, updated daily)
  - vanshb03/New-Grad-2026            (WeCracked-maintained)

Finance / quant people track these repos heavily — all three include roles
at quant firms (Jane Street, Citadel, Two Sigma, HRT, Optiver, Jump).

Table format (all three repos use the same schema):
  | Company | Role | Location | Application/Link | Date Posted |

Closed jobs are marked with ~~strikethrough~~ or 🔒 and are skipped.
"""
import logging
import re
from collections.abc import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

# Raw-content URLs for each repo's README
_REPOS: list[tuple[str, str]] = [
    (
        "SimplifyJobs/New-Grad-Positions",
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    ),
    (
        "speedyapply/2026-SWE-College-Jobs",
        "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/README.md",
    ),
    (
        "vanshb03/New-Grad-2026",
        "https://raw.githubusercontent.com/vanshb03/New-Grad-2026/main/README.md",
    ),
]

# Regex to pull the first URL out of a Markdown link: [text](URL)
_URL_RE = re.compile(r"\[.*?\]\((https?://[^)]+)\)")
# Strip any remaining markdown link syntax or icon characters from text cells
_STRIP_MD = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_ICON_RE = re.compile(r"[\U00010000-\U0010ffff]", flags=re.UNICODE)


def _clean_cell(cell: str) -> str:
    """Strip markdown links, emoji, and extra whitespace from a table cell."""
    cell = _STRIP_MD.sub(r"\1", cell)
    cell = _ICON_RE.sub("", cell)
    return cell.strip()


def _parse_table_rows(markdown: str) -> list[tuple[str, str, str]]:
    """Extract (company, title, url) tuples from a markdown job table.

    Skips:
    - Header and separator rows
    - Rows with strikethrough text (~~closed~~)
    - Rows with 🔒 (Simplify closed-application marker)
    - Rows with no parseable application URL
    """
    results = []
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        if "---" in line:
            continue
        # Skip closed postings
        if "~~" in line or "🔒" in line:
            continue

        cols = [c.strip() for c in line.split("|")]
        # Need at least: | company | title | location | link |
        if len(cols) < 5:
            continue

        company = _clean_cell(cols[1])
        title = _clean_cell(cols[2])

        # Skip header rows (cells literally say "Company" / "Role")
        if company.lower() in ("company", "") or title.lower() in ("role", "title", "position", ""):
            continue

        # Link is usually in col 4 (index 4), sometimes col 5
        link_col = cols[4] if len(cols) > 4 else cols[3]
        url_match = _URL_RE.search(link_col)
        if not url_match:
            continue

        url = url_match.group(1)
        if company and title:
            results.append((company, title, url))

    return results


class GitHubNewGradSource(Source):
    """Discover new-grad job listings from community GitHub repos."""

    name = "github_newgrad"

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        if not keywords:
            return

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for repo_name, raw_url in _REPOS:
                try:
                    async with session.get(raw_url) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "github_newgrad: %s returned HTTP %d — skipping",
                                repo_name, resp.status,
                            )
                            continue
                        text = await resp.text()
                except Exception as e:
                    logger.warning("github_newgrad: failed to fetch %s: %s", repo_name, e)
                    continue

                rows = _parse_table_rows(text)
                found = 0
                for company, title, url in rows:
                    if self._matches(title, keywords):
                        found += 1
                        yield DiscoveredJob(
                            url=url, title=title, company=company, source=self.name
                        )

                logger.info("github_newgrad: %s → %d matching jobs", repo_name, found)
