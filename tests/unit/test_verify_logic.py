"""Unit tests for pure-logic helpers in bot/verify.py.

Browser-driven verification (detect_job_closed, scan_form_errors, etc.) lives in
tests/e2e/test_verify_e2e.py. These two helpers are pure list/dict logic and
don't need a real Chromium — keeping them here makes the e2e suite browser-only
and lets these run in the fast unit pass.
"""
from bot.verify import classify_missing, missing_required_fields
from bot.models import FormField


def _field(label, required=True, answer="filled"):
    return FormField(label=label, field_type="text", required=required,
                     selector=f"#{label}", answer=answer)


def test_missing_required_when_not_submitted():
    """Required field with an answer that never appeared in submitted_fields is flagged."""
    fields = [_field("Email"), _field("Phone", required=False)]
    submitted = {}
    missing = missing_required_fields(fields, submitted)
    assert "Email" in missing


def test_no_missing_when_all_required_submitted():
    fields = [_field("Email"), _field("Name")]
    submitted = {"Email": "a@b.com", "Name": "Alice"}
    missing = missing_required_fields(fields, submitted)
    assert missing == []


def test_optional_fields_not_in_missing():
    fields = [_field("Email"), _field("Cover Letter", required=False)]
    submitted = {"Email": "a@b.com"}
    missing = missing_required_fields(fields, submitted)
    assert "Cover Letter" not in missing


def test_missing_unanswered_required_not_flagged():
    """Required fields with no answer are not flagged — they were never fillable."""
    fields = [_field("Email", answer=None), _field("Name", answer="")]
    submitted = {}
    missing = missing_required_fields(fields, submitted)
    assert missing == []


def test_classify_missing_blocks_on_email():
    blocking, warnings = classify_missing(["Email", "LinkedIn"])
    assert "Email" in blocking
    assert "LinkedIn" in warnings


def test_classify_missing_blocks_on_resume():
    blocking, _ = classify_missing(["Resume", "Phone"])
    assert "Resume" in blocking


def test_classify_missing_empty_is_empty():
    blocking, warnings = classify_missing([])
    assert blocking == []
    assert warnings == []
