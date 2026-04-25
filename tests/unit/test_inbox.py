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


# ── offer ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,body", [
    ("We'd like to extend an offer", "Please find your offer letter attached"),
    ("Job offer — Senior Engineer", "We are pleased to offer you the position"),
    ("Your offer of employment", "compensation package and start date details"),
    ("Offer letter from Acme Corp", "excited to offer you the role"),
])
def test_classify_offer(subject, body):
    assert classify_email(_make(subject, body)) == "offer"


def test_offer_beats_interview_signals():
    # "schedule" is an interview signal but offer should win
    email = _make(
        "Offer letter — please confirm start date",
        "We are pleased to offer you the position. Let's schedule an onboarding call.",
    )
    assert classify_email(email) == "offer"


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


# ── edge cases ────────────────────────────────────────────────────────────────

def test_classify_empty_email():
    assert classify_email(_make("", "")) == "other"


def test_classify_offer_body_only():
    email = _make("Following up", "We are pleased to offer you the position")
    assert classify_email(email) == "offer"


def test_classify_interview_body_only():
    email = _make("Following up", "please share your availability for a phone screen")
    assert classify_email(email) == "interview"


def test_classify_unicode_no_crash():
    result = classify_email(_make(
        "Félicitations — entretien prévu",
        "Nous sommes heureux de vous inviter",
    ))
    assert isinstance(result, str)


def test_rejection_beats_offer_and_interview():
    email = _make(
        "Update on your application",
        "Unfortunately we cannot proceed. We'd like to offer you the position but we "
        "will not be moving forward. Please share your availability anyway.",
    )
    assert classify_email(email) == "rejection"
