import asyncio
import json
import pytest
from unittest.mock import patch, MagicMock
from bot.db import ApplicationDB
from bot.models import ApplicationRecord


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_db_path):
    d = ApplicationDB(tmp_db_path)
    run(d.init())
    return d


def test_insert_and_retrieve_title(db):
    """Round-trip: inserted title is retrievable."""
    record = ApplicationRecord(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Backend Engineer",
        company="Acme Corp",
        site="greenhouse",
        status="applied",
        submitted_fields=json.dumps({"First Name": "Jane", "Email": "jane@example.com"}),
        screenshot_path="data/screenshots/test.png",
        applied_at="2025-01-01T12:00:00",
        notes="",
    )
    app_id = run(db.insert_application(record))
    fetched = run(db.get_by_id(app_id))
    assert fetched.title == "Backend Engineer"


def test_insert_and_retrieve_site(db):
    """Round-trip: site field is preserved."""
    record = ApplicationRecord(
        url="https://boards.greenhouse.io/acme/jobs/2",
        title="Backend Engineer",
        company="Acme Corp",
        site="greenhouse",
        status="applied",
    )
    app_id = run(db.insert_application(record))
    fetched = run(db.get_by_id(app_id))
    assert fetched.site == "greenhouse"


def test_insert_and_retrieve_submitted_fields(db):
    """Round-trip: JSON submitted_fields are stored and parsed correctly."""
    record = ApplicationRecord(
        url="https://boards.greenhouse.io/acme/jobs/3",
        title="Backend Engineer",
        company="Acme Corp",
        site="greenhouse",
        status="applied",
        submitted_fields=json.dumps({"First Name": "Jane", "Email": "jane@example.com"}),
    )
    app_id = run(db.insert_application(record))
    fetched = run(db.get_by_id(app_id))
    fields = json.loads(fetched.submitted_fields)
    assert fields["First Name"] == "Jane"


def _make_mock_run(responses):
    """Return a side_effect function that yields successive stdout values."""
    it = iter(responses)

    def side_effect(args, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = next(it)
        m.stderr = ""
        return m

    return side_effect


def test_needs_user_input_cover_letter_flagged(valid_profile):
    """When LLM returns NEEDS_USER_INPUT for Cover Letter, it is detected."""
    _, profile = valid_profile
    from bot.models import FormField
    from bot.llm import generate_field_answer

    fields = [
        FormField(label="Cover Letter", field_type="textarea", required=True, selector="textarea"),
        FormField(label="First Name", field_type="text", required=True, selector="#first"),
    ]

    with patch("subprocess.run", side_effect=_make_mock_run(["NEEDS_USER_INPUT:Cover Letter", "Jane"])):
        answers = [(f.label, asyncio.run(generate_field_answer(f.label, "", profile))) for f in fields]

    needs_input = [label for label, answer in answers if answer.startswith("NEEDS_USER_INPUT:")]
    assert "Cover Letter" in needs_input


def test_needs_user_input_first_name_resolved(valid_profile):
    """When LLM provides a direct answer for First Name, it is not flagged as needing input."""
    _, profile = valid_profile
    from bot.models import FormField
    from bot.llm import generate_field_answer

    fields = [
        FormField(label="Cover Letter", field_type="textarea", required=True, selector="textarea"),
        FormField(label="First Name", field_type="text", required=True, selector="#first"),
    ]

    with patch("subprocess.run", side_effect=_make_mock_run(["NEEDS_USER_INPUT:Cover Letter", "Jane"])):
        answers = [(f.label, asyncio.run(generate_field_answer(f.label, "", profile))) for f in fields]

    resolved = [label for label, answer in answers if not answer.startswith("NEEDS_USER_INPUT:")]
    assert "First Name" in resolved
