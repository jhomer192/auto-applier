"""Job discovery sources — plug-in scrapers that feed the queue.

Each source discovers job postings from a specific channel and yields
DiscoveredJob objects.  The pipeline in main.py polls all active sources
periodically and enqueues anything that hasn't been seen before.

Sources:
  github_newgrad  — parse community-maintained GitHub new-grad job repos
  company_pages   — poll Greenhouse/Lever JSON APIs for curated companies
  github_orgs     — discover hiring companies via GitHub org metadata (needs GITHUB_TOKEN)
  handshake       — campus/new-grad roles via Handshake GraphQL (needs login session)
  yc_batch        — hiring YC companies from W26/S25/W25 via the YC public API
"""
from bot.sources.base import DiscoveredJob, Source
from bot.sources.github_newgrad import GitHubNewGradSource
from bot.sources.company_pages import CompanyPagesSource
from bot.sources.github_orgs import GitHubOrgsSource
from bot.sources.handshake import HandshakeSource
from bot.sources.yc_batch import YCBatchSource

ALL_SOURCES: list[Source] = [
    GitHubNewGradSource(),
    CompanyPagesSource(),
    GitHubOrgsSource(),
    HandshakeSource(),
    YCBatchSource(),
]

SOURCE_MAP: dict[str, Source] = {s.name: s for s in ALL_SOURCES}

__all__ = ["ALL_SOURCES", "SOURCE_MAP", "Source", "DiscoveredJob"]
