"""Base class and shared types for job discovery sources."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from collections.abc import AsyncIterator


@dataclass
class DiscoveredJob:
    """A job posting found by a discovery source."""
    url: str
    title: str
    company: str
    source: str  # name of the Source that found this job


class Source(ABC):
    """Abstract job discovery source.

    Subclasses implement ``discover`` to yield DiscoveredJob objects whose
    titles match at least one keyword from the provided list.  The caller
    is responsible for deduplication (via db.is_job_seen) and enqueuing.
    """

    name: str  # unique identifier — override in each subclass

    @abstractmethod
    def discover(self, keywords: list[str]) -> AsyncIterator[DiscoveredJob]:
        """Yield jobs whose titles contain at least one keyword.

        Args:
            keywords: Desired role keywords from the user's profile
                      (e.g. ["Software Engineer", "Backend Engineer"]).

        Yields:
            DiscoveredJob for each matching, open posting found.
        """
        ...

    def _matches(self, title: str, keywords: list[str]) -> bool:
        """Return True if the title contains any keyword (case-insensitive)."""
        title_lower = title.lower()
        return any(kw.lower() in title_lower for kw in keywords)
