"""Discover hiring companies by searching GitHub organizations.

Strategy:
  1. Search GitHub for orgs with active repos in relevant topics
     (machine-learning, fintech, infrastructure, distributed-systems, etc.)
  2. For each org, check whether they have a Greenhouse or Lever job board
     by hitting the respective API endpoints (these return 200 on valid boards)
  3. If a board exists, yield matching open jobs

This finds companies that aren't on the curated list in company_pages.py —
especially smaller, high-growth orgs that are easy to miss.

Requires: GITHUB_TOKEN env var (optional — unauthenticated requests are rate-
limited to 60/hour, which is too low for broad org discovery).  If the token
is absent, this source silently no-ops so it never blocks the others.
"""
import logging
import os
from collections.abc import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

# GitHub topic queries to search for interesting engineering orgs
_TOPICS = [
    "machine-learning",
    "fintech",
    "distributed-systems",
    "infrastructure",
    "developer-tools",
    "quantitative-finance",
    "open-source",
]

_GH_SEARCH = (
    "https://api.github.com/search/repositories"
    "?q=topic:{topic}+stars:>200&sort=stars&per_page=30"
)
_GH_BOARD = "https://api.greenhouse.io/v1/boards/{slug}/jobs"
_LV_BOARD = "https://api.lever.co/v0/postings/{slug}?mode=json"


class GitHubOrgsSource(Source):
    """Discover companies via GitHub org metadata, then check their job boards."""

    name = "github_orgs"

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            logger.debug("github_orgs: GITHUB_TOKEN not set — skipping")
            return
        if not keywords:
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            checked_orgs: set[str] = set()

            for topic in _TOPICS:
                url = _GH_SEARCH.format(topic=topic)
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "github_orgs: GitHub search returned %d for topic %s",
                                resp.status, topic,
                            )
                            continue
                        data = await resp.json()
                except Exception as e:
                    logger.warning("github_orgs: GitHub search error for %s: %s", topic, e)
                    continue

                for repo in data.get("items", []):
                    owner = repo.get("owner", {})
                    if owner.get("type") != "Organization":
                        continue

                    org_login = owner.get("login", "").lower()
                    if not org_login or org_login in checked_orgs:
                        continue
                    checked_orgs.add(org_login)

                    # Try Greenhouse first (faster, most common)
                    gh_found = False
                    try:
                        async with session.get(
                            _GH_BOARD.format(slug=org_login),
                            headers={},  # Greenhouse doesn't need GitHub auth
                        ) as resp:
                            if resp.status == 200:
                                board_data = await resp.json()
                                for job in board_data.get("jobs", []):
                                    title = job.get("title", "")
                                    if self._matches(title, keywords):
                                        job_url = job.get("absolute_url", "")
                                        if job_url:
                                            gh_found = True
                                            yield DiscoveredJob(
                                                url=job_url,
                                                title=title,
                                                company=repo.get("owner", {}).get("login", org_login),
                                                source=self.name,
                                            )
                    except Exception:
                        pass

                    if gh_found:
                        continue  # Already found jobs, skip Lever check

                    # Try Lever
                    try:
                        async with session.get(
                            _LV_BOARD.format(slug=org_login),
                            headers={},
                        ) as resp:
                            if resp.status == 200:
                                lv_data = await resp.json()
                                if isinstance(lv_data, list):
                                    for job in lv_data:
                                        title = job.get("text", "")
                                        if self._matches(title, keywords):
                                            job_url = job.get("hostedUrl", "")
                                            if job_url:
                                                yield DiscoveredJob(
                                                    url=job_url,
                                                    title=title,
                                                    company=repo.get("owner", {}).get("login", org_login),
                                                    source=self.name,
                                                )
                    except Exception:
                        pass
