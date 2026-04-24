import re

from bot.adapters.base import SiteAdapter
from bot.adapters.greenhouse import GreenhouseAdapter
from bot.adapters.lever import LeverAdapter
from bot.adapters.linkedin import LinkedInAdapter


class AdapterRegistry:
    """Registry that maps job board URLs to their corresponding site adapter."""

    def __init__(self, linkedin_auth_state: str = "data/linkedin_auth.json") -> None:
        self._adapters: list[SiteAdapter] = [
            LinkedInAdapter(linkedin_auth_state),
            GreenhouseAdapter(),
            LeverAdapter(),
        ]

    def get(self, url: str) -> SiteAdapter | None:
        """Return the first adapter whose url_pattern matches *url*, or None."""
        for adapter in self._adapters:
            if re.search(adapter.url_pattern, url):
                return adapter
        return None
