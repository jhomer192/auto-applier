import pytest
from bot.adapters import AdapterRegistry
from bot.adapters.linkedin import LinkedInAdapter
from bot.adapters.greenhouse import GreenhouseAdapter
from bot.adapters.lever import LeverAdapter


@pytest.fixture
def registry():
    return AdapterRegistry(linkedin_auth_state="data/linkedin_auth.json")


def test_linkedin_url_matches(registry):
    url = "https://www.linkedin.com/jobs/view/1234567890"
    adapter = registry.get(url)
    assert adapter is not None


def test_linkedin_url_returns_linkedin_adapter(registry):
    url = "https://www.linkedin.com/jobs/view/1234567890"
    adapter = registry.get(url)
    assert isinstance(adapter, LinkedInAdapter)


def test_greenhouse_url_matches(registry):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    adapter = registry.get(url)
    assert adapter is not None


def test_greenhouse_url_returns_greenhouse_adapter(registry):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    adapter = registry.get(url)
    assert isinstance(adapter, GreenhouseAdapter)


def test_lever_url_matches(registry):
    url = "https://jobs.lever.co/acme/abc-123"
    adapter = registry.get(url)
    assert adapter is not None


def test_lever_url_returns_lever_adapter(registry):
    url = "https://jobs.lever.co/acme/abc-123"
    adapter = registry.get(url)
    assert isinstance(adapter, LeverAdapter)


def test_unknown_url_returns_none_example(registry):
    assert registry.get("https://example.com/careers") is None


def test_unknown_url_returns_none_workday(registry):
    assert registry.get("https://workday.com/job/123") is None


def test_unknown_url_returns_none_google(registry):
    assert registry.get("https://google.com") is None


def test_linkedin_profile_url_does_not_match(registry):
    # Must match /jobs/view/<digits>, not /in/<username>
    assert registry.get("https://linkedin.com/in/janedoe") is None
