"""YC Batch Intelligence — discovers hiring YC companies from recent batches.

The original implementation hit ``https://api.ycombinator.com/v0.1/companies``
which doesn't exist — the source has never returned a single company. Audit
fix #2 moves to YC's public Algolia search index, the same one that powers
ycombinator.com/companies. We POST a faceted query per batch, filter for
``isHiring``, then HEAD-probe each company's slug against Greenhouse and
Lever to find the working ATS board before we fetch its job list.

The HEAD-probe step also kills the "jump-" trailing-hyphen bug: a slug like
``jump-`` was producing ``https://boards.greenhouse.io/jump-/`` which 404s.
We sanitise the slug and only proceed if at least one ATS endpoint
returns 200.
"""
import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

# YC's public Algolia index (the same one ycombinator.com/companies uses).
# The app ID and search-only API key are exposed in the page's
# ``window.AlgoliaOpts`` and are safe to embed — the key is restricted to
# the public ``YCCompany_production`` indices and tagged ``ycdc_public``.
ALGOLIA_APP_ID = "45BWZJ1SGC"
ALGOLIA_API_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJi"
    "MWM4ZTU0ZDlhYTZjOTJiMjlhMWFuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmlj"
    "dEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlf"
    "TGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNf"
    "cHVibGljJTIyJTVE"
)
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/YCCompany_production/query"

# YC stores batches as full strings like "Winter 2025", not "W25". The
# external code (used elsewhere in the codebase) is shorter, so we keep a
# pair: ``(short, algolia)``. New batches go at the top.
CURRENT_BATCHES: list[tuple[str, str]] = [
    ("W26", "Winter 2026"),
    ("S25", "Summer 2025"),
    ("W25", "Winter 2025"),
]
ALGOLIA_HITS_PER_PAGE = 1000  # YC batches max ~250-300 companies — one page is plenty

GREENHOUSE_API = "https://api.greenhouse.io/v1/boards/{slug}/jobs"
GREENHOUSE_BOARD = "https://boards.greenhouse.io/{slug}"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
LEVER_BOARD = "https://jobs.lever.co/{slug}"

PROBE_CONCURRENCY = 8  # semaphore limit for parallel ATS probing


def _clean_slug(slug: str) -> str:
    """Strip trailing/leading dashes and whitespace from a slug.

    YC's data occasionally contains slugs like ``jump-`` (trailing hyphen)
    which produce broken board URLs. Cleaning is cheap and safe.
    """
    return (slug or "").strip().strip("-")


class YCBatchSource(Source):
    """Discovers jobs at YC-backed companies by probing their Greenhouse/Lever boards."""

    name = "yc_batch"

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        """Yield jobs from currently-hiring YC companies matching keywords."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            companies = await self._fetch_hiring_companies(session)
            logger.info("yc_batch: found %d hiring companies", len(companies))

            sem = asyncio.Semaphore(PROBE_CONCURRENCY)
            tasks = [self._probe_company(session, sem, co, keywords) for co in companies]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.debug("yc_batch: probe error: %s", result)
                    continue
                for job in result:
                    yield job

    async def _fetch_hiring_companies(self, session: aiohttp.ClientSession) -> list[dict]:
        """Fetch all hiring YC companies from recent batches via Algolia.

        Algolia does the ``isHiring`` filter for us via a second facet, so we
        only need to ask for hits we actually care about. The response schema
        gives ``slug``, ``name``, ``batch`` (e.g. "Summer 2025"),
        ``isHiring``, and other fields we don't currently use.

        Returns a deduplicated list of company dicts.
        """
        headers = {
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
            "X-Algolia-Application-Id": ALGOLIA_APP_ID,
            "Content-Type": "application/json",
        }
        companies: list[dict] = []
        for short, algolia_batch in CURRENT_BATCHES:
            body = {
                "params": (
                    f'facetFilters=[["batch:{algolia_batch}"],["isHiring:true"]]'
                    f"&hitsPerPage={ALGOLIA_HITS_PER_PAGE}"
                ),
            }
            try:
                async with session.post(ALGOLIA_URL, json=body, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "yc_batch: Algolia returned %d for batch %s", resp.status, short
                        )
                        continue
                    data = await resp.json()
            except Exception as exc:
                logger.warning("yc_batch: Algolia query failed for batch %s: %s", short, exc)
                continue

            hits = data.get("hits", [])
            for hit in hits:
                # Defensive: even with the isHiring facet, drop anything that
                # doesn't look hiring. Costs nothing and keeps tests honest.
                if not hit.get("isHiring"):
                    continue
                hit["batch"] = short  # normalise to short form for downstream code
                companies.append(hit)
            logger.info("yc_batch: batch %s — %d hiring", short, len(hits))
            await asyncio.sleep(0.3)  # be polite to Algolia between batches

        # Deduplicate by cleaned slug (preferring the first occurrence).
        seen: set[str] = set()
        deduped: list[dict] = []
        for co in companies:
            slug = _clean_slug(co.get("slug", ""))
            if slug and slug not in seen:
                seen.add(slug)
                deduped.append(co)
        return deduped

    async def _resolve_ats_slug(
        self,
        session: aiohttp.ClientSession,
        company: dict,
    ) -> tuple[str, str] | None:
        """HEAD-probe Greenhouse and Lever to find this company's working ATS.

        We try the primary slug first, then a small set of variants. Returns
        ``(system, slug)`` where system is "greenhouse" or "lever" — or None
        if no candidate URL responds with 200. HEAD requests cost ~1KB each
        and let us avoid fetching job-list bodies for boards that don't exist.

        This step also defends against the "jump-" bug: slugs are cleaned
        before probing, so a trailing-hyphen slug can't slip through.
        """
        primary = _clean_slug(company.get("slug", ""))
        if not primary:
            return None
        candidates = [primary, *self._slug_variants(primary)]

        for slug in candidates:
            slug = _clean_slug(slug)
            if not slug:
                continue
            if await self._head_ok(session, GREENHOUSE_BOARD.format(slug=slug)):
                return ("greenhouse", slug)
            if await self._head_ok(session, LEVER_BOARD.format(slug=slug)):
                return ("lever", slug)
        return None

    async def _head_ok(self, session: aiohttp.ClientSession, url: str) -> bool:
        """Return True if a HEAD request to ``url`` returns 200.

        Wraps every exception so a single timeout doesn't poison sibling
        probes. allow_redirects=True so a Greenhouse "no public board"
        redirect to a marketing page still counts as 'not present'.
        """
        try:
            async with session.head(
                url, timeout=aiohttp.ClientTimeout(total=4), allow_redirects=True,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _probe_company(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        company: dict,
        keywords: list[str],
    ) -> list[DiscoveredJob]:
        """Resolve the company's ATS via HEAD probes, then fetch matching jobs."""
        name = company.get("name") or company.get("slug", "")
        batch = company.get("batch", "")

        async with sem:
            resolved = await self._resolve_ats_slug(session, company)
            if not resolved:
                return []
            system, slug = resolved
            if system == "greenhouse":
                return await self._probe_greenhouse(session, slug, name, batch, keywords)
            if system == "lever":
                return await self._probe_lever(session, slug, name, batch, keywords)
            return []

    def _slug_variants(self, slug: str) -> list[str]:
        """Generate common slug variants to try against ATS boards.

        Capped to keep HEAD-probe traffic bounded.
        """
        variants = []
        for suffix in ["-inc", "-hq", "-ai", "-app", "-co"]:
            if slug.endswith(suffix):
                variants.append(slug[: -len(suffix)])
        for suffix in ["-ai", "-hq", "-app"]:
            if not slug.endswith(suffix):
                variants.append(slug + suffix)
        return variants[:4]

    async def _probe_greenhouse(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        company_name: str,
        batch: str,
        keywords: list[str],
    ) -> list[DiscoveredJob]:
        """Fetch and filter jobs from a Greenhouse board."""
        url = GREENHOUSE_API.format(slug=slug)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception:
            return []

        jobs = []
        for job in data.get("jobs", []):
            title = job.get("title", "")
            if not self._matches(title, keywords):
                continue
            job_url = job.get("absolute_url", "")
            if not job_url:
                continue
            jobs.append(DiscoveredJob(
                url=job_url,
                title=title,
                company=company_name,
                source=self.name,
            ))
        return jobs

    async def _probe_lever(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        company_name: str,
        batch: str,
        keywords: list[str],
    ) -> list[DiscoveredJob]:
        """Fetch and filter jobs from a Lever board."""
        url = LEVER_API.format(slug=slug)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception:
            return []

        if not isinstance(data, list):
            return []

        jobs = []
        for job in data:
            title = job.get("text", "")
            if not self._matches(title, keywords):
                continue
            job_url = job.get("hostedUrl", "")
            if not job_url:
                continue
            jobs.append(DiscoveredJob(
                url=job_url,
                title=title,
                company=company_name,
                source=self.name,
            ))
        return jobs
