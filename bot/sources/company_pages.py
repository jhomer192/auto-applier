"""Poll curated company job boards via Greenhouse and Lever JSON APIs.

Both platforms expose unauthenticated REST APIs that return clean JSON —
no scraping, no HTML parsing, no auth required.

Greenhouse API:  https://api.greenhouse.io/v1/boards/{slug}/jobs
Lever API:       https://api.lever.co/v0/postings/{slug}?mode=json

Companies are grouped by category:
  - Big tech / well-known startups
  - Infrastructure / dev-tools
  - Finance / quant firms (Jane Street, Citadel, Two Sigma, etc.)

Finance people historically monitor these boards manually or via scripts
targeting the same Greenhouse/Lever endpoints — this automates that.
"""
import logging
from collections.abc import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

# (greenhouse_slug, display_name)
_GREENHOUSE_COMPANIES: list[tuple[str, str]] = [
    # Big tech / large startups
    ("stripe", "Stripe"),
    ("airbnb", "Airbnb"),
    ("lyft", "Lyft"),
    ("dropbox", "Dropbox"),
    ("coinbase", "Coinbase"),
    ("reddit", "Reddit"),
    ("robinhood", "Robinhood"),
    ("brex", "Brex"),
    ("plaid", "Plaid"),
    ("databricks", "Databricks"),
    ("figma", "Figma"),
    ("notion", "Notion"),
    ("airtable", "Airtable"),
    ("asana", "Asana"),
    ("zendesk", "Zendesk"),
    ("cloudflare", "Cloudflare"),
    ("hashicorp", "HashiCorp"),
    ("confluent", "Confluent"),
    ("mongodb", "MongoDB"),
    ("elastic", "Elastic"),
    # AI / ML
    ("anthropic", "Anthropic"),
    ("huggingface", "Hugging Face"),
    ("cohere", "Cohere"),
    ("scale", "Scale AI"),
    ("weights-and-biases", "Weights & Biases"),
    # Finance / quant
    ("twosigma", "Two Sigma"),
    ("citadelsecurities", "Citadel Securities"),
    ("deshaw", "D.E. Shaw"),
    ("janestreet", "Jane Street"),
    ("akuna", "Akuna Capital"),
    ("imc", "IMC Trading"),
    ("simplex", "Simplex"),
    # Healthcare / biotech
    ("tempus", "Tempus"),
    ("flatiron", "Flatiron Health"),
]

# (lever_slug, display_name)
_LEVER_COMPANIES: list[tuple[str, str]] = [
    # Dev-tools / infra
    ("linear", "Linear"),
    ("vercel", "Vercel"),
    ("railway", "Railway"),
    ("render", "Render"),
    ("dbt-labs", "dbt Labs"),
    ("retool", "Retool"),
    ("postman", "Postman"),
    ("zapier", "Zapier"),
    # AI / ML
    ("openai", "OpenAI"),
    ("anyscale", "Anyscale"),
    ("modal-labs", "Modal"),
    ("together", "Together AI"),
    # Finance / quant
    ("hudsonrivertrading", "Hudson River Trading"),
    ("optiver", "Optiver"),
    ("virtu", "Virtu Financial"),
    ("jump-", "Jump Trading"),
    ("gs", "Goldman Sachs"),
    ("citadel", "Citadel"),
    # General
    ("netflix", "Netflix"),
    ("spotify", "Spotify"),
    ("canva", "Canva"),
    ("duolingo", "Duolingo"),
    ("ramp", "Ramp"),
    ("rippling", "Rippling"),
    ("gusto", "Gusto"),
]

_GH_BASE = "https://api.greenhouse.io/v1/boards/{slug}/jobs"
_LV_BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"


class CompanyPagesSource(Source):
    """Discover jobs by polling Greenhouse and Lever APIs for curated companies."""

    name = "company_pages"

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        if not keywords:
            return

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # ── Greenhouse ────────────────────────────────────────────────────
            for slug, company in _GREENHOUSE_COMPANIES:
                url = _GH_BASE.format(slug=slug)
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                except Exception as e:
                    logger.debug("company_pages: greenhouse %s error: %s", slug, e)
                    continue

                for job in data.get("jobs", []):
                    title = job.get("title", "")
                    if self._matches(title, keywords):
                        job_url = job.get("absolute_url", "")
                        if job_url:
                            yield DiscoveredJob(
                                url=job_url, title=title, company=company,
                                source=self.name,
                            )

            # ── Lever ─────────────────────────────────────────────────────────
            for slug, company in _LEVER_COMPANIES:
                url = _LV_BASE.format(slug=slug)
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                except Exception as e:
                    logger.debug("company_pages: lever %s error: %s", slug, e)
                    continue

                if not isinstance(data, list):
                    continue

                for job in data:
                    title = job.get("text", "")
                    if self._matches(title, keywords):
                        job_url = job.get("hostedUrl", "")
                        if job_url:
                            yield DiscoveredJob(
                                url=job_url, title=title, company=company,
                                source=self.name,
                            )
