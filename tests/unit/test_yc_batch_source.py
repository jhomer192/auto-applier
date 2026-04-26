"""Unit tests for bot.sources.yc_batch.YCBatchSource."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.sources.yc_batch import YCBatchSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_greenhouse_response(jobs: list[dict]) -> dict:
    return {"jobs": jobs}


def _make_greenhouse_job(title: str, url: str) -> dict:
    return {"title": title, "absolute_url": url}


def _make_lever_job(title: str, url: str) -> dict:
    return {"text": title, "hostedUrl": url}


def _make_company(slug: str, name: str = "", batch: str = "W25") -> dict:
    return {"slug": slug, "name": name or slug.title(), "batch": batch}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


SOURCE = YCBatchSource()
KEYWORDS = ["Software Engineer", "Backend"]


# ---------------------------------------------------------------------------
# _slug_variants
# ---------------------------------------------------------------------------

def test_slug_variants_removes_ai_suffix():
    variants = SOURCE._slug_variants("openai-ai")
    assert "openai" in variants


def test_slug_variants_adds_hq():
    variants = SOURCE._slug_variants("stripe")
    assert "stripe-hq" in variants


def test_slug_variants_caps_at_four():
    variants = SOURCE._slug_variants("acme")
    assert len(variants) <= 4


def test_slug_variants_removes_inc_suffix():
    variants = SOURCE._slug_variants("acme-inc")
    assert "acme" in variants


# ---------------------------------------------------------------------------
# _probe_greenhouse
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_greenhouse_success():
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=_make_greenhouse_response([
        _make_greenhouse_job("Software Engineer", "https://stripe.greenhouse.io/jobs/1"),
        _make_greenhouse_job("Backend Engineer", "https://stripe.greenhouse.io/jobs/2"),
        _make_greenhouse_job("Marketing Manager", "https://stripe.greenhouse.io/jobs/3"),
    ]))

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_greenhouse(session, "stripe", "Stripe", "W25", KEYWORDS)
    assert len(jobs) == 2
    assert all(j.company == "Stripe" for j in jobs)
    assert all(j.source == "yc_batch" for j in jobs)
    titles = {j.title for j in jobs}
    assert titles == {"Software Engineer", "Backend Engineer"}


@pytest.mark.asyncio
async def test_probe_greenhouse_no_match():
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=_make_greenhouse_response([
        _make_greenhouse_job("Marketing Manager", "https://stripe.greenhouse.io/jobs/3"),
        _make_greenhouse_job("Head of Sales", "https://stripe.greenhouse.io/jobs/4"),
    ]))

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_greenhouse(session, "stripe", "Stripe", "W25", KEYWORDS)
    assert jobs == []


@pytest.mark.asyncio
async def test_probe_greenhouse_404():
    resp = AsyncMock()
    resp.status = 404

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_greenhouse(session, "notacompany", "NotACompany", "W25", KEYWORDS)
    assert jobs == []


# ---------------------------------------------------------------------------
# _probe_lever
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_lever_success():
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=[
        _make_lever_job("Software Engineer", "https://jobs.lever.co/acme/abc123"),
        _make_lever_job("Designer", "https://jobs.lever.co/acme/def456"),
    ])

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_lever(session, "acme", "Acme", "S25", KEYWORDS)
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].url == "https://jobs.lever.co/acme/abc123"
    assert jobs[0].company == "Acme"
    assert jobs[0].source == "yc_batch"


@pytest.mark.asyncio
async def test_probe_lever_404():
    resp = AsyncMock()
    resp.status = 404

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_lever(session, "notacompany", "NotACompany", "W25", KEYWORDS)
    assert jobs == []


@pytest.mark.asyncio
async def test_probe_lever_non_list_response():
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"error": "not found"})

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False)))

    jobs = await SOURCE._probe_lever(session, "acme", "Acme", "W25", KEYWORDS)
    assert jobs == []


# ---------------------------------------------------------------------------
# _fetch_hiring_companies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hiring_companies_paginates():
    """When totalPages=2, the source should make two requests for that batch."""
    page_1 = {
        "companies": [_make_company("co-alpha"), _make_company("co-beta")],
        "totalPages": 2,
    }
    page_2 = {
        "companies": [_make_company("co-gamma")],
        "totalPages": 2,
    }

    def fake_get(url, **kwargs):
        resp = AsyncMock()
        resp.status = 200
        # Only respond to W25 batch (last batch checked) with paginated data
        if "batch=W26" in url:
            resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
        elif "batch=S25" in url:
            resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
        elif "page=1" in url and "batch=W25" in url:
            resp.json = AsyncMock(return_value=page_1)
        elif "page=2" in url and "batch=W25" in url:
            resp.json = AsyncMock(return_value=page_2)
        else:
            resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
        return AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False))

    session = MagicMock()
    session.get = fake_get

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    slugs = {co["slug"] for co in companies}
    assert "co-alpha" in slugs
    assert "co-beta" in slugs
    assert "co-gamma" in slugs
    assert len(companies) == 3


@pytest.mark.asyncio
async def test_fetch_hiring_companies_deduplicates():
    """A slug appearing in both W26 and S25 should appear only once."""
    shared_slug = "double-co"

    def fake_get(url, **kwargs):
        resp = AsyncMock()
        resp.status = 200
        if "batch=W26" in url:
            resp.json = AsyncMock(return_value={
                "companies": [_make_company(shared_slug, batch="W26")],
                "totalPages": 1,
            })
        elif "batch=S25" in url:
            resp.json = AsyncMock(return_value={
                "companies": [_make_company(shared_slug, batch="S25")],
                "totalPages": 1,
            })
        else:
            resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
        return AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False))

    session = MagicMock()
    session.get = fake_get

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    assert len([co for co in companies if co["slug"] == shared_slug]) == 1


# ---------------------------------------------------------------------------
# discover (end-to-end)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_yields_via_greenhouse():
    """Full end-to-end: fetch companies -> probe greenhouse -> yield job."""
    yc_company = _make_company("linear-app", name="Linear", batch="W25")

    greenhouse_resp = AsyncMock()
    greenhouse_resp.status = 200
    greenhouse_resp.json = AsyncMock(return_value=_make_greenhouse_response([
        _make_greenhouse_job("Software Engineer", "https://linear.greenhouse.io/jobs/42"),
    ]))

    lever_resp = AsyncMock()
    lever_resp.status = 404

    yc_api_resp = AsyncMock()
    yc_api_resp.status = 200

    call_num = 0

    def fake_get(url, **kwargs):
        nonlocal call_num
        call_num += 1

        if "ycombinator.com" in url:
            resp = AsyncMock()
            resp.status = 200
            if call_num == 1:
                # First batch (W26): no companies
                resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
            elif call_num == 2:
                # Second batch (S25): no companies
                resp.json = AsyncMock(return_value={"companies": [], "totalPages": 1})
            else:
                # Third batch (W25): one company
                resp.json = AsyncMock(return_value={"companies": [yc_company], "totalPages": 1})
            return AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock(return_value=False))

        if "greenhouse.io" in url and "linear-app" in url:
            return AsyncMock(__aenter__=AsyncMock(return_value=greenhouse_resp), __aexit__=AsyncMock(return_value=False))

        # Everything else (lever, slug variants) -> 404
        not_found = AsyncMock()
        not_found.status = 404
        return AsyncMock(__aenter__=AsyncMock(return_value=not_found), __aexit__=AsyncMock(return_value=False))

    mock_session = MagicMock()
    mock_session.get = fake_get
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.sources.yc_batch.aiohttp.ClientSession", return_value=mock_session), \
         patch("asyncio.sleep", new=AsyncMock()):
        jobs = []
        async for job in SOURCE.discover(["Software Engineer"]):
            jobs.append(job)

    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].company == "Linear"
    assert jobs[0].source == "yc_batch"
    assert jobs[0].url == "https://linear.greenhouse.io/jobs/42"
