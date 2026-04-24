import asyncio
import pytest
import tempfile
import os


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def valid_profile(tmp_path):
    import yaml
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF fake")
    profile = {
        "name": "Jane Smith",
        "email": "jane@example.com",
        "phone": "+1-555-000-0000",
        "location": "San Francisco, CA",
        "resume_path": str(resume),
        "work_history": [
            {"title": "Engineer", "company": "Acme", "start": "2021-06", "end": "present", "description": "Built things"}
        ],
        "education": [{"degree": "B.S. CS", "school": "MIT", "year": "2021"}],
        "skills": ["Python", "SQL"],
    }
    p = tmp_path / "profile.yaml"
    with open(p, "w") as f:
        yaml.dump(profile, f)
    return str(p), profile
