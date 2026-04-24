"""Job search scrapers.

Polls LinkedIn job search results using an authenticated Playwright session.
Returns a list of (title, company, url) tuples for unseen jobs matching the query.
"""
import logging
import urllib.parse

from bot.models import SavedSearch, SearchResult

logger = logging.getLogger(__name__)

# Max jobs to inspect per search poll (keeps each poll fast)
_MAX_RESULTS_PER_POLL = 20


async def search_linkedin(
    search: SavedSearch,
    auth_state_path: str = "data/linkedin_auth.json",
) -> list[SearchResult]:
    """Scrape LinkedIn job search for fresh results.

    Uses the authenticated Playwright session (same linkedin_auth.json the
    adapter uses) so no extra login is needed.

    Args:
        search: SavedSearch config (query, location).
        auth_state_path: Path to the LinkedIn Playwright auth state file.

    Returns:
        List of SearchResult for each job card found on the first results page.
    """
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError as e:
        logger.error("Playwright not installed: %s", e)
        return []

    params = {"keywords": search.query, "f_LF": "f_AL"}  # f_AL = Easy Apply
    if search.location:
        params["location"] = search.location
    search_url = "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

    results: list[SearchResult] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(storage_state=auth_state_path)
            except Exception:
                # Auth state missing or invalid — skip gracefully
                logger.warning("LinkedIn auth state not found at %s — skipping search", auth_state_path)
                await browser.close()
                return []

            page = await ctx.new_page()
            await stealth_async(page)

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # Job cards on the search results page
                cards = await page.query_selector_all("a.job-card-list__title, a.job-card-container__link")
                if not cards:
                    # Alternate selector for newer LinkedIn layout
                    cards = await page.query_selector_all("[data-job-id] a[href*='/jobs/view/']")

                seen_urls: set[str] = set()
                for card in cards[:_MAX_RESULTS_PER_POLL]:
                    try:
                        href = await card.get_attribute("href") or ""
                        if "/jobs/view/" not in href:
                            continue
                        # Normalise — strip query params
                        url = "https://www.linkedin.com" + href.split("?")[0] if href.startswith("/") else href.split("?")[0]
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)

                        title = (await card.inner_text()).strip() or "Unknown Title"

                        # Try to get company from nearest ancestor card
                        company = "Unknown"
                        parent = await card.evaluate_handle(
                            "el => el.closest('[data-job-id]') || el.closest('.job-card-container')"
                        )
                        if parent:
                            company_el = await parent.query_selector(
                                ".job-card-container__company-name, "
                                ".artdeco-entity-lockup__subtitle, "
                                ".job-card-list__company-name"
                            )
                            if company_el:
                                company = (await company_el.inner_text()).strip() or "Unknown"

                        results.append(SearchResult(
                            title=title,
                            company=company,
                            url=url,
                            search_id=search.id or 0,
                        ))
                    except Exception as card_err:
                        logger.debug("Error parsing job card: %s", card_err)
                        continue

            except Exception as nav_err:
                logger.error("LinkedIn search navigation error: %s", nav_err)
            finally:
                await browser.close()

    except Exception as e:
        logger.error("search_linkedin error: %s", e)

    logger.info("search_linkedin: found %d results for %r", len(results), search.query)
    return results
