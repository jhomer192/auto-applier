import pytest
from bot.inbox import classify_email
from bot.models import EmailThread


def _make(subject: str, body: str = "") -> EmailThread:
    return EmailThread(
        message_id="<test@example.com>",
        thread_id="<test@example.com>",
        from_address="recruiter@company.com",
        subject=subject,
        body_preview=body,
        direction="inbound",
    )


# ── interview ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,body", [
    ("Interview Invitation — Senior Engineer", ""),
    ("We'd like to schedule a phone screen", ""),
    ("Next steps for your application", "We'd love to connect"),
    ("Availability for a video call?", "Can you share some times?"),
    ("Technical screen — please share your availability", ""),
    ("Moving forward with your application", "We are excited to invite you"),
])
def test_classify_interview(subject, body):
    assert classify_email(_make(subject, body)) == "interview"


# ── rejection ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,body", [
    ("Update on your application", "Unfortunately, we will not be moving forward"),
    ("Your application at Acme", "We have decided to pursue other candidates"),
    ("Thank you for your interest", "We regret to inform you that we are not a fit"),
    ("Application status update", "The position has been filled"),
])
def test_classify_rejection(subject, body):
    assert classify_email(_make(subject, body)) == "rejection"


# ── confirmation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,body", [
    ("Application received — Software Engineer", "We received your application and will be in touch"),
    ("Thank you for applying to Acme Corp", "Your application has been successfully submitted"),
    ("Application confirmation", "We'll be in touch once we've reviewed your application"),
])
def test_classify_confirmation(subject, body):
    assert classify_email(_make(subject, body)) == "confirmation"


# ── other ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,body", [
    ("Follow-up on your application", ""),
    ("Quick question about your experience", ""),
    ("Hello from the recruiting team", ""),
])
def test_classify_other(subject, body):
    assert classify_email(_make(subject, body)) == "other"


# ── rejection beats interview signals ────────────────────────────────────────

def test_rejection_beats_interview_signals():
    # "next steps" is an interview signal but "unfortunately" should win
    email = _make(
        "An update on next steps",
        "Unfortunately, we will not be moving forward with your application.",
    )
    assert classify_email(email) == "rejection"
