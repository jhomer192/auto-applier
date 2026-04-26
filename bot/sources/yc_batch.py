"""YC Batch Intelligence — discovers hiring YC companies from recent batches."""
import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

YC_API_BASE = "https://api.ycombinator.com/v0.1/companies"
CURRENT_BATCHES = ["W26", "S25", "W25"]  # most recent first
GREENHOUSE_API = "https://api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
PROBE_CONCURRENCY = 8  # semaphore limit for parallel ATS probing


class YCBatchSource(Source):
    """Discovers jobs at YC-backed companies by probing their Greenhouse/Lever boards."""

    name = "yc_batch"

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        """Yield jobs from currently-hiring YC companies matching keywords.

        Args:
            keywords: Desired role keywords from the user's profile
                      (e.g. ["Software Engineer", "Backend Engineer"]).

        Yields:
            DiscoveredJob for each matching, open posting found.
        """
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
        """Fetch all hiring YC companies from recent batches.

        Args:
            session: The aiohttp session to use for requests.

        Returns:
            Deduplicated list of company dicts from the YC API.
        """
        companies = []
        for batch in CURRENT_BATCHES:
            page = 1
            while True:
                url = f"{YC_API_BASE}?batch={batch}&isHiring=true&page={page}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                except Exception as exc:
                    logger.warning("yc_batch: failed to fetch batch %s page %d: %s", batch, page, exc)
                    break

                batch_companies = data.get("companies", [])
                if not batch_companies:
                    break

                for co in batch_companies:
                    co["batch"] = batch  # ensure batch field is present
                companies.extend(batch_companies)

                if page >= data.get("totalPages", 1):
                    break
                page += 1
                await asyncio.sleep(0.3)

        # Deduplicate by slug
        seen: set[str] = set()
        deduped = []
        for co in companies:
            slug = co.get("slug", "")
            if slug and slug not in seen:
                seen.add(slug)
                deduped.append(co)
        return deduped

    async def _probe_company(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        company: dict,
        keywords: list[str],
    ) -> list[DiscoveredJob]:
        """Try Greenhouse then Lever for a company's job board.

        Args:
            session: The aiohttp session to use.
            sem: Semaphore controlling probe concurrency.
            company: Company dict from YC API.
            keywords: Role keywords to filter by.

        Returns:
            List of matching DiscoveredJob objects (may be empty).
        """
        slug = company.get("slug", "")
        name = company.get("name", slug)
        batch = company.get("batch", "")
        if not slug:
            return []

        async with sem:
            # Try primary slug on Greenhouse
            jobs = await self._probe_greenhouse(session, slug, name, batch, keywords)
            if jobs:
                return jobs

            # Try slug variants on Greenhouse
            for variant in self._slug_variants(slug):
                jobs = await self._probe_greenhouse(session, variant, name, batch, keywords)
                if jobs:
                    return jobs

            # Try primary slug on Lever
            jobs = await self._probe_lever(session, slug, name, batch, keywords)
            if jobs:
                return jobs

            # Try slug variants on Lever
            for variant in self._slug_variants(slug):
                jobs = await self._probe_lever(session, variant, name, batch, keywords)
                if jobs:
                    return jobs

            return []

    def _slug_variants(self, slug: str) -> list[str]:
        """Generate common slug variants to try against ATS boards.

        Args:
            slug: The primary YC slug for the company.

        Returns:
            Up to 4 alternative slug strings.
        """
        variants = []
        # Remove common suffixes
        for suffix in ["-inc", "-hq", "-ai", "-app", "-co"]:
            if slug.endswith(suffix):
                variants.append(slug[: -len(suffix)])
        # Add common suffixes
        for suffix in ["-ai", "-hq", "-app"]:
            if not slug.endswith(suffix):
                variants.append(slug + suffix)
        return variants[:4]  # cap to avoid excessive requests

    async def _probe_greenhouse(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        company_name: str,
        batch: str,
        keywords: list[str],
    ) -> list[DiscoveredJob]:
        """Fetch and filter jobs from a Greenhouse board.

        Args:
            session: The aiohttp session to use.
            slug: Board slug to query.
            company_name: Human-readable company name.
            batch: YC batch string (e.g. "W25").
            keywords: Role keywords to filter by.

        Returns:
            Matching DiscoveredJob objects.
        """
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
        """Fetch and filter jobs from a Lever board.

        Args:
            session: The aiohttp session to use.
            slug: Lever posting slug to query.
            company_name: Human-readable company name.
            batch: YC batch string (e.g. "W25").
            keywords: Role keywords to filter by.

        Returns:
            Matching DiscoveredJob objects.
        """
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
