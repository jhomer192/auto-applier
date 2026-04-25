"""Tests for bot/ratelimit.py — enforce_rate_limit() and RateLimitExceeded."""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from bot.ratelimit import RateLimitExceeded, enforce_rate_limit
from bot.models import ApplicationRecord


def _make_db(recent_records: list[ApplicationRecord]) -> MagicMock:
    """Return a mock DB whose get_recent() yields the given records."""
    db = MagicMock()
    db.get_recent = AsyncMock(return_value=recent_records)
    return db


def _applied_record(applied_at: str) -> ApplicationRecord:
    return ApplicationRecord(
        url="https://example.com/job/1",
        title="Engineer",
        company="Acme",
        site="greenhouse",
        status="applied",
        applied_at=applied_at,
    )


def _skipped_record(applied_at: str) -> ApplicationRecord:
    return ApplicationRecord(
        url="https://example.com/job/2",
        title="Designer",
        company="Acme",
        site="lever",
        status="skipped",
        applied_at=applied_at,
    )


class TestNoApplications:
    """When there are no prior applications the limiter should return immediately."""

    @pytest.mark.asyncio
    async def test_no_applications_returns_immediately(self):
        db = _make_db([])
        # Should complete without sleeping
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_skipped_records_returns_immediately(self):
        now_str = datetime.now(timezone.utc).isoformat()
        db = _make_db([_skipped_record(now_str)])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()


class TestDailyCap:
    """Daily cap enforcement."""

    @pytest.mark.asyncio
    async def test_raises_when_cap_reached(self):
        now = datetime.now(timezone.utc)
        today_str = now.isoformat()
        records = [_applied_record(today_str) for _ in range(10)]
        db = _make_db(records)
        with pytest.raises(RateLimitExceeded, match="Daily cap"):
            await enforce_rate_limit(db, min_gap_minutes=1, max_gap_minutes=2, daily_cap=10)

    @pytest.mark.asyncio
    async def test_does_not_raise_below_cap(self):
        now = datetime.now(timezone.utc)
        records = [_applied_record(now.isoformat()) for _ in range(9)]
        db = _make_db(records)
        # Should not raise even though there are recent applications
        # (they're all "now" so the gap would be ~0 — we just want no RateLimitExceeded)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await enforce_rate_limit(db, min_gap_minutes=0, max_gap_minutes=0, daily_cap=10)

    @pytest.mark.asyncio
    async def test_yesterday_applications_not_counted_toward_cap(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        records = [_applied_record(yesterday) for _ in range(15)]
        db = _make_db(records)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            # 15 applications yesterday, cap is 10 — should NOT raise because they're yesterday
            await enforce_rate_limit(db, min_gap_minutes=0, max_gap_minutes=0, daily_cap=10)

    @pytest.mark.asyncio
    async def test_error_message_includes_cap_value(self):
        now_str = datetime.now(timezone.utc).isoformat()
        records = [_applied_record(now_str) for _ in range(5)]
        db = _make_db(records)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await enforce_rate_limit(db, min_gap_minutes=1, max_gap_minutes=2, daily_cap=5)
        assert "5" in str(exc_info.value)


class TestGapEnforcement:
    """Time-gap enforcement between submissions."""

    @pytest.mark.asyncio
    async def test_no_wait_when_gap_already_passed(self):
        # Last application was 10 minutes ago, gap is 4–8 minutes
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db = _make_db([_applied_record(past)])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_sleeps_when_last_application_was_recent(self):
        # Last application was 1 minute ago, gap is 4–8 minutes → expect sleep
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db = _make_db([_applied_record(past)])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_called_once()
        sleep_secs = mock_sleep.call_args[0][0]
        # Should sleep roughly 3–7 minutes (180–420 seconds, give ±5s for float rounding)
        assert 170 <= sleep_secs <= 430, f"Unexpected sleep duration: {sleep_secs}s"

    @pytest.mark.asyncio
    async def test_sleep_within_gap_bounds(self):
        # Last application was exactly 0 seconds ago — max possible wait = max_gap minutes
        just_now = datetime.now(timezone.utc).isoformat()
        db = _make_db([_applied_record(just_now)])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=2, max_gap_minutes=5, daily_cap=30)
        if mock_sleep.called:
            secs = mock_sleep.call_args[0][0]
            assert 0 <= secs <= 5 * 60 + 5, f"Sleep out of bounds: {secs}s"

    @pytest.mark.asyncio
    async def test_only_applied_status_used_for_gap(self):
        # The most recent record is "skipped", then an old "applied"
        old_applied = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        just_now = datetime.now(timezone.utc).isoformat()
        db = _make_db([
            _skipped_record(just_now),    # skipped — should be ignored
            _applied_record(old_applied), # applied 30 min ago — gap already passed
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_naive_timestamp_treated_as_utc(self):
        """Timestamps stored without timezone info should be assumed UTC."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        naive_past = past.replace("+00:00", "")  # strip tz info
        db = _make_db([_applied_record(naive_past)])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_timestamp_skips_gap_check(self):
        record = _applied_record("not-a-real-timestamp")
        db = _make_db([record])
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Should not raise, should not sleep
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30)
        mock_sleep.assert_not_called()


class TestNotifyCallback:
    """Optional notify callback."""

    @pytest.mark.asyncio
    async def test_notify_called_when_waiting(self):
        just_now = datetime.now(timezone.utc).isoformat()
        db = _make_db([_applied_record(just_now)])
        notify = AsyncMock()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30, notify=notify)
        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "Pacing" in msg or "waiting" in msg.lower()

    @pytest.mark.asyncio
    async def test_notify_not_called_when_no_wait_needed(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        db = _make_db([_applied_record(old)])
        notify = AsyncMock()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30, notify=notify)
        notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_message_includes_time(self):
        just_now = datetime.now(timezone.utc).isoformat()
        db = _make_db([_applied_record(just_now)])
        received_messages = []

        async def capture(msg):
            received_messages.append(msg)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await enforce_rate_limit(db, min_gap_minutes=4, max_gap_minutes=8, daily_cap=30, notify=capture)

        assert len(received_messages) == 1
        # Should contain time info — either "m" (minutes) or "s" (seconds)
        assert "m" in received_messages[0] or "s" in received_messages[0]


class TestRateLimitExceeded:
    """RateLimitExceeded exception class."""

    def test_is_exception(self):
        exc = RateLimitExceeded("test message")
        assert isinstance(exc, Exception)

    def test_message_preserved(self):
        exc = RateLimitExceeded("Daily cap of 30 reached")
        assert "30" in str(exc)
