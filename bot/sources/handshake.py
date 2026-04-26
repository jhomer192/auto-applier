"""Handshake job discovery source — campus/new-grad roles via GraphQL API."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

import aiohttp

from bot.sources.base import DiscoveredJob, Source

logger = logging.getLogger(__name__)

HANDSHAKE_GRAPHQL = "https://app.joinhandshake.com/graphql"
PAGE_SIZE = 25
MAX_PAGES = 4

POSTINGS_QUERY = """
query PostingSearch($query: String, $page: Int, $per_page: Int) {
  postings(query: $query, page: $page, per_page: $per_page, filters: {job_type_names: ["Full-Time"]}) {
    total_count
    items {
      id
      title
      employer { name }
      apply_url
      created_at
    }
  }
}
"""


class HandshakeSource(Source):
    """Discover campus and new-grad job postings via the Handshake GraphQL API.

    Requires a saved auth state from setup/handshake_login.py.  When the auth
    file is absent the source silently skips.  When the session expires (HTTP
    401/403 or a GraphQL auth error) the source sets ``_session_expired`` so
    the /handshake Telegram command can surface a re-auth prompt.
    """

    name = "handshake"

    def __init__(self) -> None:
        self._auth_path = os.getenv("HANDSHAKE_AUTH_STATE", "data/handshake_auth.json")
        self._session_expired = False

    def _is_active(self) -> bool:
        """Return True if an auth state file exists on disk."""
        return Path(self._auth_path).exists()

    def _load_cookies(self) -> tuple[dict, str]:
        """Load cookies from auth state JSON.

        Returns:
            Tuple of (cookies_dict, csrf_token).  Expired cookies (past
            Unix timestamp, not session cookies) are filtered out.
        """
        state = json.loads(Path(self._auth_path).read_text())
        now = time.time()
        cookies: dict[str, str] = {}
        for c in state.get("cookies", []):
            # expires == -1 means session cookie — keep those
            if c.get("expires", -1) != -1 and c.get("expires", 1) < now:
                continue
            cookies[c["name"]] = c["value"]

        csrf = (
            cookies.get("_csrf_token")
            or cookies.get("CSRF-TOKEN")
            or cookies.get("csrf_token")
            or ""
        )
        return cookies, csrf

    async def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        """Yield DiscoveredJob objects from Handshake matching any keyword.

        Args:
            keywords: Role keywords to search for (e.g. ["Software Engineer"]).

        Yields:
            DiscoveredJob for each matching Full-Time posting found.
        """
        if not self._is_active():
            logger.debug("handshake: no auth state — source inactive")
            return

        try:
            cookies, csrf = self._load_cookies()
        except Exception as e:
            logger.warning("handshake: failed to load auth state: %s", e)
            return

        seen_ids: set[str] = set()
        headers = {
            "Content-Type": "application/json",
            "X-Csrf-Token": csrf,
            "Referer": "https://app.joinhandshake.com/postings",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        }

        async with aiohttp.ClientSession(headers=headers, cookies=cookies) as session:
            for keyword in keywords:
                page = 1
                while page <= MAX_PAGES:
                    payload = {
                        "query": POSTINGS_QUERY,
                        "variables": {
                            "query": keyword,
                            "page": page,
                            "per_page": PAGE_SIZE,
                        },
                    }
                    try:
                        async with session.post(
                            HANDSHAKE_GRAPHQL,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status in (401, 403):
                                logger.warning(
                                    "handshake: session expired (HTTP %d)", resp.status
                                )
                                self._session_expired = True
                                return
                            data = await resp.json()
                    except Exception as e:
                        logger.warning("handshake: request failed: %s", e)
                        break

                    # Surface GraphQL-level auth failures
                    if "errors" in data:
                        err_msg = str(data["errors"])
                        if "authenticated" in err_msg.lower() or "authorized" in err_msg.lower():
                            logger.warning(
                                "handshake: GraphQL auth error: %s", err_msg
                            )
                            self._session_expired = True
                            return
                        logger.warning("handshake: GraphQL errors: %s", err_msg)
                        break

                    postings = data.get("data", {}).get("postings", {})
                    items = postings.get("items", [])
                    if not items:
                        break

                    for item in items:
                        job_id = str(item.get("id", ""))
                        if not job_id or job_id in seen_ids:
                            continue
                        title = item.get("title", "")
                        if not self._matches(title, keywords):
                            continue
                        seen_ids.add(job_id)
                        company = (item.get("employer") or {}).get("name", "")
                        url = f"https://app.joinhandshake.com/postings/{job_id}"
                        yield DiscoveredJob(
                            url=url,
                            title=title,
                            company=company,
                            source=self.name,
                        )

                    total = postings.get("total_count", 0)
                    if page * PAGE_SIZE >= total:
                        break
                    page += 1
                    await asyncio.sleep(0.5)
