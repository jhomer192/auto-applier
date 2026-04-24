import pytest
from bot.scraper import is_eeo_field, field_answer_hint
from bot.models import FormField


# ── is_eeo_field ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Gender", "Race/Ethnicity", "Are you a veteran?",
    "Disability status", "Hispanic or Latino", "EEO information",
])
def test_is_eeo_field_positive(label):
    assert is_eeo_field(label), f"Expected {label!r} to be detected as EEO field"


@pytest.mark.parametrize("label", [
    "First Name", "Email", "Years of Experience", "Cover Letter",
    "LinkedIn Profile", "Resume", "Phone Number",
])
def test_is_eeo_field_negative(label):
    assert not is_eeo_field(label), f"Expected {label!r} NOT to be an EEO field"


# ── field_answer_hint ─────────────────────────────────────────────────────────

def test_hint_select_includes_options():
    field = FormField(
        label="Employment Type",
        field_type="select",
        required=True,
        selector="#emp_type",
        options=["Full-time", "Part-time", "Contract"],
    )
    hint = field_answer_hint(field)
    assert hint is not None
    assert "Full-time" in hint
    assert "Part-time" in hint
    assert "dropdown" in hint.lower()


def test_hint_checkbox_guidance():
    field = FormField(
        label="I agree to the terms",
        field_type="checkbox",
        required=True,
        selector="#agree",
    )
    hint = field_answer_hint(field)
    assert hint is not None
    assert "checkbox" in hint.lower()
    assert "yes" in hint.lower() or "no" in hint.lower()


def test_hint_eeo_field_returns_decline():
    field = FormField(
        label="Gender",
        field_type="select",
        required=False,
        selector="#gender",
        options=["Male", "Female", "Non-binary", "Decline to self-identify"],
    )
    hint = field_answer_hint(field)
    assert hint is not None
    assert "Decline" in hint or "decline" in hint


def test_hint_salary_field_returns_needs_user_input():
    field = FormField(
        label="Desired Salary",
        field_type="text",
        required=False,
        selector="#salary",
    )
    hint = field_answer_hint(field)
    assert hint is not None
    assert "NEEDS_USER_INPUT" in hint


def test_hint_visa_field():
    field = FormField(
        label="Are you authorized to work in the US?",
        field_type="select",
        required=True,
        selector="#visa",
        options=["Yes", "No"],
    )
    hint = field_answer_hint(field)
    assert hint is not None


def test_hint_plain_text_field_returns_none():
    field = FormField(
        label="Cover Letter",
        field_type="textarea",
        required=False,
        selector="#cover",
    )
    hint = field_answer_hint(field)
    assert hint is None


def test_hint_select_caps_options_at_20():
    """Options list should be truncated to 20 in the hint."""
    options = [f"Option {i}" for i in range(30)]
    field = FormField(
        label="Category",
        field_type="select",
        required=False,
        selector="#cat",
        options=options,
    )
    hint = field_answer_hint(field)
    assert hint is not None
    # The hint should mention exactly 20 options, not 30
    assert "Option 20" not in hint or hint.count('"Option') <= 20
