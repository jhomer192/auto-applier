"""Unit tests for bot.sources.handshake.HandshakeSource."""
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.sources.handshake import HandshakeSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_posting(job_id: str, title: str, employer_name: str = "Acme") -> dict:
    return {
        "id": job_id,
        "title": title,
        "employer": {"name": employer_name},
        "apply_url": f"https://app.joinhandshake.com/postings/{job_id}",
        "created_at": "2026-04-01T00:00:00Z",
    }


def _gql_response(items: list[dict], total_count: int | None = None) -> dict:
    return {
        "data": {
            "postings": {
                "total_count": total_count if total_count is not None else len(items),
                "items": items,
            }
        }
    }


def _write_auth(path: Path, cookies: list[dict]) -> None:
    path.write_text(json.dumps({"cookies": cookies}))


def _valid_cookie(name: str, value: str, expires: int = -1) -> dict:
    return {"name": name, "value": value, "expires": expires}


def _expired_cookie(name: str, value: str) -> dict:
    # Expired 1 day ago
    return {"name": name, "value": value, "expires": int(time.time()) - 86400}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIsActive:
    def test_inactive_when_auth_missing(self, tmp_path):
        src = HandshakeSource()
        src._auth_path = str(tmp_path / "missing.json")
        assert src._is_active() is False

    def test_active_when_auth_present(self, tmp_path):
        auth = tmp_path / "auth.json"
        _write_auth(auth, [])
        src = HandshakeSource()
        src._auth_path = str(auth)
        assert src._is_active() is True


class TestLoadCookies:
    def test_load_cookies_filters_expired(self, tmp_path):
        auth = tmp_path / "auth.json"
        cookies = [
            _expired_cookie("old_cookie", "stale"),
            _valid_cookie("fresh_cookie", "alive"),
        ]
        _write_auth(auth, cookies)

        src = HandshakeSource()
        src._auth_path = str(auth)
        loaded, _ = src._load_cookies()

        assert "fresh_cookie" in loaded
        assert "old_cookie" not in loaded

    def test_load_cookies_keeps_session_cookies(self, tmp_path):
        """Cookies with expires=-1 are session cookies and must be kept."""
        auth = tmp_path / "auth.json"
        cookies = [_valid_cookie("session_tok", "abc", expires=-1)]
        _write_auth(auth, cookies)

        src = HandshakeSource()
        src._auth_path = str(auth)
        loaded, _ = src._load_cookies()

        assert loaded["session_tok"] == "abc"

    def test_load_cookies_extracts_csrf(self, tmp_path):
        auth = tmp_path / "auth.json"
        cookies = [_valid_cookie("_csrf_token", "mytoken")]
        _write_auth(auth, cookies)

        src = HandshakeSource()
        src._auth_path = str(auth)
        _, csrf = src._load_cookies()

        assert csrf == "mytoken"

    def test_load_cookies_csrf_fallback_names(self, tmp_path):
        """CSRF-TOKEN and csrf_token are also recognised."""
        for name in ("CSRF-TOKEN", "csrf_token"):
            auth = tmp_path / "auth.json"
            cookies = [_valid_cookie(name, "tok123")]
            _write_auth(auth, cookies)

            src = HandshakeSource()
            src._auth_path = str(auth)
            _, csrf = src._load_cookies()

            assert csrf == "tok123"


class TestDiscover:
    @pytest.mark.asyncio
    async def test_inactive_when_auth_missing(self, tmp_path):
        src = HandshakeSource()
        src._auth_path = str(tmp_path / "missing.json")

        results = [job async for job in src.discover(["Software Engineer"])]
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_yields_matching_jobs(self, tmp_path):
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        items = [
            _make_posting("1", "Software Engineer", "Acme"),
            _make_posting("2", "Marketing Manager", "Acme"),
        ]
        response_data = _gql_response(items)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=response_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer"])]

        assert len(results) == 1
        assert results[0].title == "Software Engineer"
        assert results[0].company == "Acme"
        assert results[0].source == "handshake"
        assert results[0].url == "https://app.joinhandshake.com/postings/1"

    @pytest.mark.asyncio
    async def test_discover_deduplicates(self, tmp_path):
        """Same job ID returned for two keywords must be yielded only once."""
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        items = [_make_posting("42", "Software Engineer", "Corp")]
        response_data = _gql_response(items)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=response_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer", "SWE"])]

        ids = [r.url for r in results]
        assert len(ids) == len(set(ids)), "duplicate jobs yielded"
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_discover_handles_401(self, tmp_path):
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer"])]

        assert results == []
        assert src._session_expired is True

    @pytest.mark.asyncio
    async def test_discover_handles_403(self, tmp_path):
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer"])]

        assert results == []
        assert src._session_expired is True

    @pytest.mark.asyncio
    async def test_discover_handles_graphql_auth_error(self, tmp_path):
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        error_data = {"errors": [{"message": "not authenticated"}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=error_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer"])]

        assert results == []
        assert src._session_expired is True

    @pytest.mark.asyncio
    async def test_discover_paginates(self, tmp_path):
        """When total_count > PAGE_SIZE the source must fetch the next page."""
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        # Page 1: 25 items, total_count=30
        page1_items = [_make_posting(str(i), "Software Engineer", "Corp") for i in range(25)]
        page1 = _gql_response(page1_items, total_count=30)

        # Page 2: 5 items, total_count=30
        page2_items = [_make_posting(str(i + 100), "Software Engineer", "Corp") for i in range(5)]
        page2 = _gql_response(page2_items, total_count=30)

        call_count = 0

        async def _json_side_effect():
            nonlocal call_count
            call_count += 1
            return page1 if call_count == 1 else page2

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = _json_side_effect
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            with patch("bot.sources.handshake.asyncio.sleep", new_callable=AsyncMock):
                results = [job async for job in src.discover(["Software Engineer"])]

        assert call_count == 2, f"Expected 2 page requests, got {call_count}"
        assert len(results) == 30

    @pytest.mark.asyncio
    async def test_discover_handles_network_error(self, tmp_path):
        """A request exception should cause the keyword loop to break, not crash."""
        auth = tmp_path / "auth.json"
        _write_auth(auth, [_valid_cookie("_csrf_token", "tok")])

        src = HandshakeSource()
        src._auth_path = str(auth)

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=Exception("connection reset"))
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("bot.sources.handshake.aiohttp.ClientSession", return_value=mock_session):
            results = [job async for job in src.discover(["Software Engineer"])]

        assert results == []
        assert src._session_expired is False
