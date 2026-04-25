"""Tests for bot/auto_apply.py — ensure_auto_searches logic."""
import asyncio
import pytest
from bot.auto_apply import ensure_auto_searches
from bot.db import ApplicationDB


def _db(tmp_db_path: str) -> ApplicationDB:
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


def _profile(roles: list[str] = None, location: str = "San Francisco, CA") -> dict:
    return {
        "name": "Jane",
        "email": "jane@example.com",
        "phone": "555-0000",
        "location": location,
        "job_preferences": {
            "desired_roles": roles or [],
            "auto_search": True,
        },
    }


def test_ensure_auto_searches_creates_for_each_role(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["Software Engineer", "Backend Engineer"])
    created = asyncio.run(ensure_auto_searches(db, profile))
    assert created == 2


def test_ensure_auto_searches_does_not_duplicate(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["Software Engineer"])
    asyncio.run(ensure_auto_searches(db, profile))
    created_again = asyncio.run(ensure_auto_searches(db, profile))
    assert created_again == 0


def test_ensure_auto_searches_case_insensitive_dedup(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["Software Engineer"])
    asyncio.run(ensure_auto_searches(db, profile))
    # Same role, different case
    profile2 = _profile(roles=["software engineer"])
    created = asyncio.run(ensure_auto_searches(db, profile2))
    assert created == 0


def test_ensure_auto_searches_no_op_when_disabled(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["Software Engineer"])
    profile["job_preferences"]["auto_search"] = False
    created = asyncio.run(ensure_auto_searches(db, profile))
    assert created == 0


def test_ensure_auto_searches_no_op_when_no_roles(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=[])
    created = asyncio.run(ensure_auto_searches(db, profile))
    assert created == 0


def test_ensure_auto_searches_caps_at_five(tmp_db_path):
    db = _db(tmp_db_path)
    roles = ["Role A", "Role B", "Role C", "Role D", "Role E", "Role F", "Role G"]
    profile = _profile(roles=roles)
    created = asyncio.run(ensure_auto_searches(db, profile))
    assert created == 5


def test_ensure_auto_searches_sets_location(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["ML Engineer"], location="New York, NY")
    asyncio.run(ensure_auto_searches(db, profile))
    searches = asyncio.run(db.get_all_searches())
    # desired_roles are lowercased by load_preferences
    ml_search = next(s for s in searches if s.query.lower() == "ml engineer")
    assert ml_search.location == "New York, NY"


def test_ensure_auto_searches_sets_active(tmp_db_path):
    db = _db(tmp_db_path)
    profile = _profile(roles=["Data Scientist"])
    asyncio.run(ensure_auto_searches(db, profile))
    searches = asyncio.run(db.get_all_searches())
    assert all(s.active for s in searches)


def test_ensure_auto_searches_partial_dedup(tmp_db_path):
    """If one role already exists, only the new ones are created."""
    db = _db(tmp_db_path)
    profile1 = _profile(roles=["Software Engineer"])
    asyncio.run(ensure_auto_searches(db, profile1))

    profile2 = _profile(roles=["Software Engineer", "Staff Engineer"])
    created = asyncio.run(ensure_auto_searches(db, profile2))
    assert created == 1  # only Staff Engineer is new
