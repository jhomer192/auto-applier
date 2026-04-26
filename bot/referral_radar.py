"""Referral Radar — LinkedIn mutual connection finder.

Scrapes LinkedIn People search to identify mutual connections at a target company,
then ranks them by relationship strength so the user can request referrals.
"""
import asyncio
import logging
import time
import urllib.parse
from typing import Optional

from bot.models import ReferralCandidate

logger = logging.getLogger(__name__)

# Simple in-memory rate limiter: max 3 calls per hour
_last_calls: list[float] = []
RATE_LIMIT = 3
RATE_WINDOW = 3600


def _truncate_note(message: str, max_chars: int = 300) -> str:
    """Hard-truncate a string to max_chars, appending '...' if truncated.

    Args:
        message: The string to potentially truncate.
        max_chars: Maximum allowed character count (inclusive). Defaults to 300.

    Returns:
        The original string if within limit, otherwise a version truncated to
        max_chars - 3 characters plus '...'.
    """
    if len(message) <= max_chars:
        return message
    return message[: max_chars - 3] + "..."


def _check_rate_limit() -> bool:
    """Return True if a new scraping call is allowed under the rate limit.

    Prunes calls older than RATE_WINDOW before checking. Modifies _last_calls
    in-place to record the current call when allowed.
    """
    now = time.monotonic()
    # Remove calls outside the rolling window
    while _last_calls and now - _last_calls[0] > RATE_WINDOW:
        _last_calls.pop(0)
    if len(_last_calls) >= RATE_LIMIT:
        return False
    _last_calls.append(now)
    return True


def _rank_connection_type(connection_type: str) -> int:
    """Return sort key for connection type (lower = higher priority).

    Args:
        connection_type: One of '1st', '2nd', 'alumni', or other string.

    Returns:
        Integer rank where 0 is highest priority.
    """
    order = {"alumni": 0, "1st": 1, "2nd": 2}
    return order.get(connection_type, 3)


async def find_referral_candidates(
    company: str,
    user_school: str,
    user_companies: list[str],
    linkedin_auth: str,
    max_results: int = 5,
    timeout: int = 30,
) -> list[ReferralCandidate]:
    """Search LinkedIn People for mutual connections at the target company.

    Uses Playwright to scrape LinkedIn search results. Returns an empty list
    on any error rather than raising, so callers are never blocked.

    Args:
        company: Target company name to search for employees at.
        user_school: Applicant's school name for alumni matching.
        user_companies: List of past/current employers for former-colleague matching.
        linkedin_auth: Path to the LinkedIn Playwright auth state JSON file.
        max_results: Maximum number of candidates to return. Defaults to 5.
        timeout: Maximum seconds to wait for page operations. Defaults to 30.

    Returns:
        List of ReferralCandidate instances ranked: alumni > 1st degree > 2nd degree > other.
        Returns [] on rate limit, missing auth, timeout, or Playwright errors.
    """
    import os
    if not os.path.exists(linkedin_auth):
        logger.warning("referral_radar: LinkedIn auth file not found at %s", linkedin_auth)
        return []

    if not _check_rate_limit():
        logger.warning("referral_radar: rate limit reached (%d calls per hour), skipping", RATE_LIMIT)
        return []

    try:
        return await asyncio.wait_for(
            _scrape_linkedin_people(company, user_school, user_companies, linkedin_auth, max_results),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("referral_radar: scrape timed out after %ds for company=%r", timeout, company)
        return []
    except Exception as exc:
        logger.warning("referral_radar: unexpected error for company=%r: %s", company, exc)
        return []


async def _scrape_linkedin_people(
    company: str,
    user_school: str,
    user_companies: list[str],
    linkedin_auth: str,
    max_results: int,
) -> list[ReferralCandidate]:
    """Internal coroutine: Playwright LinkedIn People search.

    Args:
        company: Target company name.
        user_school: Applicant's school for alumni detection.
        user_companies: Applicant's past employers for colleague detection.
        linkedin_auth: Path to auth state JSON.
        max_results: Max candidates to extract.

    Returns:
        Ranked list of ReferralCandidate instances.
    """
    from playwright.async_api import async_playwright

    encoded_company = urllib.parse.quote(company)
    search_url = (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={encoded_company}&origin=GLOBAL_SEARCH_HEADER"
    )

    candidates: list[ReferralCandidate] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=linkedin_auth)
            page = await context.new_page()

            await page.goto(search_url, wait_until="domcontentloaded")
            await page.wait_for_selector(".reusable-search__result-container", timeout=15000)

            result_cards = await page.query_selector_all(".reusable-search__result-container")

            for card in result_cards[:max_results * 3]:  # oversample, then rank+trim
                try:
                    candidate = await _extract_candidate_from_card(
                        card, company, user_school, user_companies
                    )
                    if candidate:
                        candidates.append(candidate)
                except Exception as card_err:
                    logger.debug("referral_radar: error extracting card: %s", card_err)
                    continue

                if len(candidates) >= max_results * 2:
                    break

        finally:
            await browser.close()

    # Rank: alumni > 1st > 2nd > other
    candidates.sort(key=lambda c: _rank_connection_type(c.connection_type))
    return candidates[:max_results]


async def _extract_candidate_from_card(
    card,
    company: str,
    user_school: str,
    user_companies: list[str],
) -> Optional[ReferralCandidate]:
    """Extract a ReferralCandidate from a single LinkedIn search result card.

    Args:
        card: Playwright ElementHandle for the result card.
        company: Target company name.
        user_school: Applicant's school for alumni detection.
        user_companies: Applicant's past employers for colleague detection.

    Returns:
        A ReferralCandidate if the person appears to work at the target company,
        None otherwise.
    """
    # Extract name
    name_el = await card.query_selector(".entity-result__title-text a span[aria-hidden='true']")
    if not name_el:
        name_el = await card.query_selector(".entity-result__title-text")
    name = (await name_el.inner_text()).strip() if name_el else ""
    if not name:
        return None

    # Extract headline (usually job title + company)
    headline_el = await card.query_selector(".entity-result__primary-subtitle")
    headline = (await headline_el.inner_text()).strip() if headline_el else ""

    # Only include people who appear to work at the target company
    company_lower = company.lower()
    if company_lower not in headline.lower():
        return None

    # Extract LinkedIn profile URL
    link_el = await card.query_selector("a.app-aware-link")
    linkedin_url = ""
    if link_el:
        href = await link_el.get_attribute("href")
        if href and "/in/" in href:
            # Strip query params
            linkedin_url = href.split("?")[0]

    # Extract connection degree (1st/2nd) from badge
    degree_el = await card.query_selector(".dist-value")
    degree_text = (await degree_el.inner_text()).strip() if degree_el else ""
    connection_type = ""
    if "1st" in degree_text:
        connection_type = "1st"
    elif "2nd" in degree_text:
        connection_type = "2nd"

    # Determine shared context for alumni or former colleague
    shared_name = ""
    if user_school and user_school.lower() in headline.lower():
        connection_type = "alumni"
        shared_name = user_school
    elif not shared_name and connection_type == "2nd":
        # Try to extract mutual connection name from card (if shown)
        mutual_el = await card.query_selector(".entity-result__secondary-subtitle")
        mutual_text = (await mutual_el.inner_text()).strip() if mutual_el else ""
        if mutual_text and "mutual" in mutual_text.lower():
            shared_name = mutual_text.split(" mutual")[0].strip()

    # Check former colleague
    if not connection_type or connection_type not in ("1st", "2nd", "alumni"):
        for past_company in user_companies:
            if past_company.lower() in headline.lower():
                connection_type = "2nd"
                shared_name = past_company
                break

    return ReferralCandidate(
        id=None,
        app_id=None,
        name=name,
        headline=headline,
        linkedin_url=linkedin_url,
        connection_type=connection_type,
        shared_name=shared_name,
        draft_message="",
    )
