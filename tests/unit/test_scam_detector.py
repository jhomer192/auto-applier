"""Unit tests for bot.scam_detector — pure heuristic, no I/O."""
import pytest
from bot.scam_detector import check_scam


def _long_desc(word_count: int = 200) -> str:
    """Generate a filler description of exactly word_count words."""
    return " ".join(["lorem"] * word_count)


def _short_desc(word_count: int = 50) -> str:
    return " ".join(["word"] * word_count)


class TestClean:
    def test_clean_greenhouse_url(self):
        result = check_scam(
            url="https://boards.greenhouse.io/acme/jobs/123456",
            title="Software Engineer",
            company="Acme Corp",
            raw_description=_long_desc(200),
        )
        assert result.verdict == "clean"
        assert result.score < 40

    def test_lever_url_clean(self):
        result = check_scam(
            url="https://jobs.lever.co/stripe/abc-def",
            title="Backend Engineer",
            company="Stripe",
            raw_description=_long_desc(200),
        )
        assert result.verdict == "clean"


class TestGenericEmail:
    def test_generic_gmail_email(self):
        result = check_scam(
            url="https://boards.greenhouse.io/company/jobs/1",
            title="Data Analyst",
            company="SomeCorp",
            raw_description=_long_desc(200) + " Send resume to jobs@gmail.com",
        )
        assert result.score >= 30
        assert any("Generic contact email" in s for s in result.signals)

    def test_yahoo_email_triggers(self):
        result = check_scam(
            url="https://jobs.lever.co/company/job",
            title="Manager",
            company="Company",
            raw_description=_long_desc(200) + " Contact hr@yahoo.com",
        )
        assert result.score >= 30


class TestSuspiciousTLD:
    def test_suspicious_tld_tk(self):
        result = check_scam(
            url="https://applynow.tk/job/engineer",
            title="Software Engineer",
            company="TechCorp",
            raw_description=_long_desc(200),
        )
        assert result.score >= 35
        assert any("Suspicious URL domain" in s for s in result.signals)

    def test_suspicious_tld_xyz(self):
        result = check_scam(
            url="https://jobs.xyz/posting/123",
            title="Developer",
            company="DevCo",
            raw_description=_long_desc(200),
        )
        assert result.score >= 35


class TestTTGBTPhrases:
    def test_ttgbt_phrase_work_from_home(self):
        result = check_scam(
            url="https://boards.greenhouse.io/company/jobs/1",
            title="Coordinator",
            company="Company",
            raw_description=_long_desc(200) + " work from home earn $ unlimited earning",
        )
        assert result.score >= 40
        assert any("Suspicious language" in s for s in result.signals)

    def test_single_phrase_hits(self):
        result = check_scam(
            url="https://boards.greenhouse.io/co/jobs/1",
            title="Agent",
            company="Agency",
            raw_description=_long_desc(200) + " no experience needed",
        )
        assert result.score >= 20

    def test_more_than_two_phrases_capped_at_forty(self):
        """Score contribution from TTGBT phrases is capped at 40 (max 2 hits x 20).
        Signals list may contain more than 2 phrases, but score only increments twice.
        We verify by checking the total score stays at the TTGBT cap when only those
        signals are present (greenhouse URL is known, long desc, normal company).
        """
        desc = _long_desc(200) + " work from home passive income make money fast residual income"
        result = check_scam(
            url="https://boards.greenhouse.io/co/jobs/2",
            title="Rep",
            company="Company",
            raw_description=desc,
        )
        # greenhouse.io is a known ATS — no unknown-domain penalty
        # description is long — no vague-description penalty
        # no generic email, no bad TLD, no shortener, no placeholder company, no impersonation
        # so all score should come from TTGBT, capped at 40
        assert result.score <= 40
        assert len([s for s in result.signals if s.startswith("Suspicious language")]) >= 2


class TestFaangImpersonation:
    def test_faang_impersonation_google(self):
        result = check_scam(
            url="https://scamsite.com/job/google-engineer",
            title="Google Engineer",
            company="Google",
            raw_description=_long_desc(200),
        )
        assert result.score >= 40
        assert any("impersonation" in s.lower() for s in result.signals)

    def test_no_impersonation_on_real_google(self):
        result = check_scam(
            url="https://careers.google.com/jobs/results/123",
            title="Google Software Engineer",
            company="Google",
            raw_description=_long_desc(200),
        )
        assert not any("impersonation" in s.lower() for s in result.signals)


class TestPlaceholderCompany:
    def test_placeholder_company_bare_llc(self):
        result = check_scam(
            url="https://boards.greenhouse.io/co/jobs/1",
            title="Manager",
            company="XY LLC",
            raw_description=_long_desc(200),
        )
        assert any("Unverifiable company name" in s for s in result.signals)

    def test_all_caps_short_company(self):
        result = check_scam(
            url="https://boards.greenhouse.io/co/jobs/1",
            title="Analyst",
            company="XYZ",
            raw_description=_long_desc(200),
        )
        assert any("Unverifiable company name" in s for s in result.signals)

    def test_digits_in_company_name(self):
        result = check_scam(
            url="https://boards.greenhouse.io/co/jobs/1",
            title="Engineer",
            company="Tech123",
            raw_description=_long_desc(200),
        )
        assert any("Unverifiable company name" in s for s in result.signals)


class TestVagueDescription:
    def test_vague_description_short(self):
        result = check_scam(
            url="https://boards.greenhouse.io/company/jobs/1",
            title="Engineer",
            company="SomeCo",
            raw_description=_short_desc(50),
        )
        assert result.score >= 20
        assert any("Very short job description" in s for s in result.signals)

    def test_empty_description_triggers(self):
        result = check_scam(
            url="https://boards.greenhouse.io/company/jobs/1",
            title="Engineer",
            company="SomeCo",
            raw_description="",
        )
        assert result.score >= 20


class TestCompoundSignalsRejected:
    def test_compound_signals_rejected(self):
        """Multiple strong signals together should push score to rejected (>=80)."""
        result = check_scam(
            url="https://bit.ly/fakejob",
            title="Easy Money Rep",
            company="XY LLC",
            raw_description=_short_desc(50) + " Send cv to jobs@gmail.com work from home earn $",
        )
        assert result.score >= 80
        assert result.verdict == "rejected"


class TestFlaggedBoundary:
    def test_flagged_boundary(self):
        """URL shortener alone (35) + vague description (20) = 55 => flagged."""
        result = check_scam(
            url="https://bit.ly/jobposting",
            title="Operations Role",
            company="Legitimate Co",
            raw_description=_short_desc(50),
        )
        assert 40 <= result.score < 80
        assert result.verdict == "flagged"


class TestKnownATS:
    def test_known_ats_no_penalty(self):
        """Greenhouse URL should not trigger 'Unknown hosting domain'."""
        result = check_scam(
            url="https://boards.greenhouse.io/acme/jobs/9999",
            title="Software Engineer",
            company="Acme",
            raw_description=_long_desc(200),
        )
        assert not any("Unknown hosting domain" in s for s in result.signals)

    def test_lever_ats_no_penalty(self):
        result = check_scam(
            url="https://jobs.lever.co/company/job-id",
            title="Engineer",
            company="Company",
            raw_description=_long_desc(200),
        )
        assert not any("Unknown hosting domain" in s for s in result.signals)


class TestURLShortener:
    def test_url_shortener_bitly(self):
        result = check_scam(
            url="https://bit.ly/somejob",
            title="Engineer",
            company="Company",
            raw_description=_long_desc(200),
        )
        assert result.score >= 35
        assert any("URL shortener" in s for s in result.signals)

    def test_url_shortener_tinyurl(self):
        result = check_scam(
            url="https://tinyurl.com/abcdefg",
            title="Analyst",
            company="Corp",
            raw_description=_long_desc(200),
        )
        assert result.score >= 35


class TestScoreCap:
    def test_score_capped_at_100(self):
        """Pile every possible signal on — score must not exceed 100."""
        result = check_scam(
            url="https://bit.ly/scam",  # shortener +35; also unknown = skipped because shortener already fired
            title="Google Engineer",   # FAANG impersonation +40
            company="XY LLC",          # placeholder +15
            raw_description=(
                _short_desc(50)        # vague +20
                + " jobs@gmail.com"    # generic email +30
                + " work from home passive income make money fast"  # TTGBT +40 (capped at 2)
            ),
        )
        assert result.score == 100
