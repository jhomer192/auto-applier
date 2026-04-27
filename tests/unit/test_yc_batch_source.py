"""Unit tests for ``bot.sources.yc_batch.YCBatchSource``.

The source migrated from a fictional ``api.ycombinator.com`` endpoint to
YC's real Algolia search index (audit fix #2). These tests cover:

  - Algolia query: POST body shape, batch facet, isHiring filter
  - Slug cleaning: trailing/leading hyphens stripped (the "jump-" bug)
  - HEAD probe: resolves to greenhouse / lever / None
  - End-to-end discover(): Algolia -> resolve -> fetch -> yield
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.sources.yc_batch import YCBatchSource, _clean_slug


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _algolia_response(hits: list[dict]) -> dict:
    return {"hits": hits, "nbHits": len(hits), "page": 0, "nbPages": 1}


def _hit(slug: str, name: str = "", batch: str = "W25", hiring: bool = True) -> dict:
    return {
        "slug": slug,
        "name": name or slug.title(),
        "batch": batch,
        "isHiring": hiring,
    }


def _gh_job(title: str, url: str) -> dict:
    return {"title": title, "absolute_url": url}


def _lever_job(title: str, url: str) -> dict:
    return {"text": title, "hostedUrl": url}


def _ctx_resp(resp: AsyncMock) -> AsyncMock:
    """Wrap a response in an async context manager."""
    return AsyncMock(
        __aenter__=AsyncMock(return_value=resp),
        __aexit__=AsyncMock(return_value=False),
    )


SOURCE = YCBatchSource()
KEYWORDS = ["Software Engineer", "Backend"]


# ---------------------------------------------------------------------------
# _clean_slug
# ---------------------------------------------------------------------------


def test_clean_slug_strips_trailing_hyphen():
    """The 'jump-' bug: trailing hyphen broke board URL construction."""
    assert _clean_slug("jump-") == "jump"


def test_clean_slug_strips_leading_hyphen():
    assert _clean_slug("-acme") == "acme"


def test_clean_slug_strips_whitespace():
    assert _clean_slug("  stripe  ") == "stripe"


def test_clean_slug_handles_empty():
    assert _clean_slug("") == ""
    assert _clean_slug(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _slug_variants
# ---------------------------------------------------------------------------


def test_slug_variants_removes_ai_suffix():
    assert "openai" in SOURCE._slug_variants("openai-ai")


def test_slug_variants_caps_at_four():
    assert len(SOURCE._slug_variants("acme")) <= 4


# ---------------------------------------------------------------------------
# _fetch_hiring_companies — Algolia integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_hiring_companies_uses_algolia_post():
    """One POST per batch, hitting the Algolia endpoint with the right body."""
    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("json", {})))
        resp = AsyncMock(status=200)
        resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post

    with patch("asyncio.sleep", new=AsyncMock()):
        await SOURCE._fetch_hiring_companies(session)

    # one POST per batch
    from bot.sources.yc_batch import ALGOLIA_URL, CURRENT_BATCHES
    assert len(posts) == len(CURRENT_BATCHES)
    assert all(url == ALGOLIA_URL for url, _ in posts)
    # each body carries a facet filter for its batch (full Algolia name)
    bodies = [body["params"] for _, body in posts]
    for _short, algolia_batch in CURRENT_BATCHES:
        assert any(f"batch:{algolia_batch}" in b for b in bodies)
    # and every body filters on isHiring:true (Algolia-side, not just client)
    assert all("isHiring:true" in b for b in bodies)


@pytest.mark.asyncio
async def test_fetch_hiring_companies_filters_isHiring_false():
    """Companies with isHiring=False must be dropped."""
    def fake_post(url, **kwargs):
        resp = AsyncMock(status=200)
        # Return a hiring + a non-hiring on the W26 batch only
        if "batch:Winter 2026" in kwargs["json"]["params"]:
            resp.json = AsyncMock(return_value=_algolia_response([
                _hit("alpha", batch="W26", hiring=True),
                _hit("beta", batch="W26", hiring=False),
            ]))
        else:
            resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    slugs = {c["slug"] for c in companies}
    assert "alpha" in slugs
    assert "beta" not in slugs


@pytest.mark.asyncio
async def test_fetch_hiring_companies_deduplicates_across_batches():
    """A company appearing in multiple batches should appear once."""
    shared = "acme"

    def fake_post(url, **kwargs):
        resp = AsyncMock(status=200)
        params = kwargs["json"]["params"]
        if "batch:Winter 2026" in params:
            resp.json = AsyncMock(return_value=_algolia_response([
                _hit(shared, batch="W26"),
            ]))
        elif "batch:Summer 2025" in params:
            resp.json = AsyncMock(return_value=_algolia_response([
                _hit(shared, batch="S25"),
            ]))
        else:
            resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    assert len([c for c in companies if c["slug"] == shared]) == 1


@pytest.mark.asyncio
async def test_fetch_hiring_companies_handles_5xx():
    """An Algolia error on one batch must not poison the others."""
    def fake_post(url, **kwargs):
        params = kwargs["json"]["params"]
        if "batch:Winter 2026" in params:
            resp = AsyncMock(status=500)
            return _ctx_resp(resp)
        resp = AsyncMock(status=200)
        if "batch:Summer 2025" in params:
            resp.json = AsyncMock(return_value=_algolia_response([_hit("acme", batch="S25")]))
        else:
            resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    assert any(c["slug"] == "acme" for c in companies)


@pytest.mark.asyncio
async def test_fetch_hiring_companies_cleans_dirty_slugs():
    """A trailing-hyphen slug like 'jump-' must be cleaned during dedup."""
    def fake_post(url, **kwargs):
        resp = AsyncMock(status=200)
        if "batch:Winter 2026" in kwargs["json"]["params"]:
            resp.json = AsyncMock(return_value=_algolia_response([_hit("jump-", batch="W26")]))
        else:
            resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post

    with patch("asyncio.sleep", new=AsyncMock()):
        companies = await SOURCE._fetch_hiring_companies(session)

    # Original raw slug is preserved on the dict, but it WAS counted in the
    # cleaned-slug set so a duplicate "jump" entry on a different batch
    # would be dropped.
    assert len(companies) == 1


# ---------------------------------------------------------------------------
# _resolve_ats_slug — HEAD probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_ats_slug_finds_greenhouse():
    def fake_head(url, **kwargs):
        # Greenhouse for the primary slug returns 200.
        if "boards.greenhouse.io/acme" in url:
            return _ctx_resp(AsyncMock(status=200))
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.head = fake_head

    result = await SOURCE._resolve_ats_slug(session, {"slug": "acme"})
    assert result == ("greenhouse", "acme")


@pytest.mark.asyncio
async def test_resolve_ats_slug_falls_back_to_lever():
    def fake_head(url, **kwargs):
        if "jobs.lever.co/acme" in url:
            return _ctx_resp(AsyncMock(status=200))
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.head = fake_head

    result = await SOURCE._resolve_ats_slug(session, {"slug": "acme"})
    assert result == ("lever", "acme")


@pytest.mark.asyncio
async def test_resolve_ats_slug_returns_none_if_no_board():
    """No board → no probing of the job-list endpoint, no spurious results."""
    def fake_head(url, **kwargs):
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.head = fake_head

    result = await SOURCE._resolve_ats_slug(session, {"slug": "acme"})
    assert result is None


@pytest.mark.asyncio
async def test_resolve_ats_slug_cleans_slug_before_probe():
    """A trailing-hyphen slug must be sanitised before the HEAD probe."""
    probed_urls: list[str] = []

    def fake_head(url, **kwargs):
        probed_urls.append(url)
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.head = fake_head

    await SOURCE._resolve_ats_slug(session, {"slug": "jump-"})

    # Every probed URL should contain "jump" but never "jump-/" or "jump--"
    assert probed_urls, "no probes attempted"
    for url in probed_urls:
        assert "jump-/" not in url
        assert "jump--" not in url


@pytest.mark.asyncio
async def test_resolve_ats_slug_swallows_head_exceptions():
    """A timeout on one HEAD probe must not crash the whole resolution."""
    probed = 0

    def fake_head(url, **kwargs):
        nonlocal probed
        probed += 1
        if probed == 1:
            # First probe times out
            raise asyncio.TimeoutError()
        # Subsequent ones return 404
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.head = fake_head

    # Must not raise — exception is swallowed and counted as 'no board'
    result = await SOURCE._resolve_ats_slug(session, {"slug": "acme"})
    assert result is None
    assert probed >= 2  # at least one more probe attempted after the failure


# ---------------------------------------------------------------------------
# _probe_company — uses _resolve_ats_slug under the hood
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_company_uses_resolved_system():
    """When resolve says lever, _probe_company calls _probe_lever, not greenhouse."""
    company = {"slug": "acme", "name": "Acme", "batch": "W25"}
    sem = asyncio.Semaphore(1)

    with patch.object(
        SOURCE, "_resolve_ats_slug",
        new=AsyncMock(return_value=("lever", "acme")),
    ), patch.object(
        SOURCE, "_probe_lever", new=AsyncMock(return_value=["sentinel"]),
    ), patch.object(
        SOURCE, "_probe_greenhouse", new=AsyncMock(return_value=[]),
    ):
        result = await SOURCE._probe_company(MagicMock(), sem, company, KEYWORDS)
        assert result == ["sentinel"]
        SOURCE._probe_lever.assert_awaited_once()
        SOURCE._probe_greenhouse.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_company_returns_empty_when_no_ats():
    """A company with no working ATS board yields no jobs and no fetch attempts."""
    company = {"slug": "noboard", "name": "NoBoard", "batch": "W25"}
    sem = asyncio.Semaphore(1)

    with patch.object(
        SOURCE, "_resolve_ats_slug", new=AsyncMock(return_value=None),
    ), patch.object(
        SOURCE, "_probe_greenhouse", new=AsyncMock(),
    ), patch.object(
        SOURCE, "_probe_lever", new=AsyncMock(),
    ):
        result = await SOURCE._probe_company(MagicMock(), sem, company, KEYWORDS)
        assert result == []
        SOURCE._probe_greenhouse.assert_not_awaited()
        SOURCE._probe_lever.assert_not_awaited()


# ---------------------------------------------------------------------------
# _probe_greenhouse / _probe_lever (kept from prior implementation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_greenhouse_filters_titles():
    resp = AsyncMock(status=200)
    resp.json = AsyncMock(return_value={"jobs": [
        _gh_job("Software Engineer", "https://stripe.greenhouse.io/jobs/1"),
        _gh_job("Marketing Manager", "https://stripe.greenhouse.io/jobs/2"),
    ]})

    session = MagicMock()
    session.get = MagicMock(return_value=_ctx_resp(resp))

    jobs = await SOURCE._probe_greenhouse(session, "stripe", "Stripe", "W25", KEYWORDS)
    titles = {j.title for j in jobs}
    assert titles == {"Software Engineer"}


@pytest.mark.asyncio
async def test_probe_lever_filters_titles():
    resp = AsyncMock(status=200)
    resp.json = AsyncMock(return_value=[
        _lever_job("Backend Engineer", "https://jobs.lever.co/acme/1"),
        _lever_job("Designer", "https://jobs.lever.co/acme/2"),
    ])

    session = MagicMock()
    session.get = MagicMock(return_value=_ctx_resp(resp))

    jobs = await SOURCE._probe_lever(session, "acme", "Acme", "S25", KEYWORDS)
    titles = {j.title for j in jobs}
    assert titles == {"Backend Engineer"}


# ---------------------------------------------------------------------------
# discover — full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_end_to_end():
    """Algolia returns 1 hiring company → resolve to greenhouse → yield 1 job."""
    company = _hit("linear", name="Linear", batch="W25")

    def fake_post(url, **kwargs):
        resp = AsyncMock(status=200)
        params = kwargs["json"]["params"]
        if "batch:Winter 2025" in params:
            resp.json = AsyncMock(return_value=_algolia_response([company]))
        else:
            resp.json = AsyncMock(return_value=_algolia_response([]))
        return _ctx_resp(resp)

    def fake_head(url, **kwargs):
        if "boards.greenhouse.io/linear" in url:
            return _ctx_resp(AsyncMock(status=200))
        return _ctx_resp(AsyncMock(status=404))

    def fake_get(url, **kwargs):
        if "greenhouse.io/v1/boards/linear/jobs" in url:
            resp = AsyncMock(status=200)
            resp.json = AsyncMock(return_value={"jobs": [
                _gh_job("Software Engineer", "https://linear.greenhouse.io/jobs/42"),
            ]})
            return _ctx_resp(resp)
        return _ctx_resp(AsyncMock(status=404))

    session = MagicMock()
    session.post = fake_post
    session.head = fake_head
    session.get = fake_get
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.sources.yc_batch.aiohttp.ClientSession", return_value=session), \
         patch("asyncio.sleep", new=AsyncMock()):
        jobs = []
        async for job in SOURCE.discover(["Software Engineer"]):
            jobs.append(job)

    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].company == "Linear"
    assert jobs[0].source == "yc_batch"
    assert jobs[0].url == "https://linear.greenhouse.io/jobs/42"


@pytest.mark.asyncio
async def test_discover_emits_nothing_when_algolia_returns_no_hiring():
    """Sanity: end-to-end with zero hiring companies must yield zero jobs."""
    def fake_post(url, **kwargs):
        resp = AsyncMock(status=200)
        # Return one non-hiring company on every batch
        resp.json = AsyncMock(return_value=_algolia_response([
            _hit("snoozeco", hiring=False),
        ]))
        return _ctx_resp(resp)

    session = MagicMock()
    session.post = fake_post
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.sources.yc_batch.aiohttp.ClientSession", return_value=session), \
         patch("asyncio.sleep", new=AsyncMock()):
        jobs = []
        async for job in SOURCE.discover(["Software Engineer"]):
            jobs.append(job)

    assert jobs == []
