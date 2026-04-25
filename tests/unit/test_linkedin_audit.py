"""Tests for bot/linkedin_audit.py — HTML stripping, response parsing, formatting."""
import pytest
from bot.linkedin_audit import (
    _strip_html,
    _truncate_profile_text,
    _parse_audit_response,
    format_audit_report,
    AuditReport,
    AuditSection,
)


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags():
    html = "<h1>Jane Smith</h1><p>Software Engineer</p>"
    result = _strip_html(html)
    assert "Jane Smith" in result
    assert "Software Engineer" in result
    assert "<" not in result


def test_strip_html_removes_script():
    html = "<body><script>alert('xss')</script><p>Hello</p></body>"
    result = _strip_html(html)
    assert "alert" not in result
    assert "Hello" in result


def test_strip_html_removes_style():
    html = "<style>body { color: red; }</style><p>Content</p>"
    result = _strip_html(html)
    assert "color" not in result
    assert "Content" in result


def test_strip_html_decodes_entities():
    html = "<p>5 &amp; 6 &lt; 12 &gt; 3 &nbsp;space&nbsp;</p>"
    result = _strip_html(html)
    assert "&amp;" not in result
    assert "5 & 6" in result


def test_strip_html_deduplicates_lines():
    html = "<p>Repeated</p><p>Repeated</p><p>Different</p>"
    result = _strip_html(html)
    lines = [l for l in result.splitlines() if l.strip()]
    repeated = [l for l in lines if l.strip() == "Repeated"]
    assert len(repeated) == 1


def test_strip_html_collapses_blank_lines():
    html = "<p>A</p>\n\n\n<p>B</p>"
    result = _strip_html(html)
    blank_runs = [l for l in result.splitlines() if not l.strip()]
    assert len(blank_runs) == 0  # no blank lines in output


# ---------------------------------------------------------------------------
# _truncate_profile_text
# ---------------------------------------------------------------------------


def test_truncate_no_op_when_short():
    text = "short text"
    assert _truncate_profile_text(text, max_chars=100) == text


def test_truncate_trims_long_text():
    text = "x" * 10000
    result = _truncate_profile_text(text, max_chars=500)
    assert len(result) <= 520  # 500 + some trailing marker text
    assert "truncated" in result


def test_truncate_preserves_exact_boundary():
    text = "y" * 6000
    result = _truncate_profile_text(text, max_chars=6000)
    assert result == text


# ---------------------------------------------------------------------------
# _parse_audit_response
# ---------------------------------------------------------------------------


_SAMPLE_RESPONSE = """
OVERALL_SCORE: 62
OVERALL_VERDICT: Decent foundation but experience bullets need quantification.

SECTION: Headline
SCORE: 7
VERDICT: Good
SUGGESTION: Add your top skill stack after the title.
SUGGESTION: Include seniority level for ATS matching.

SECTION: Summary
SCORE: 4
VERDICT: Weak
SUGGESTION: Open with a compelling 1-sentence story, not job duties.
SUGGESTION: Add at least one quantified achievement.

SECTION: Experience
SCORE: 6
VERDICT: Good
SUGGESTION: Use STAR format: Situation, Task, Action, Result.
SUGGESTION: Replace "responsible for" with active verbs.

SECTION: Skills
SCORE: 5
VERDICT: Weak
SUGGESTION: Move Python and SQL to the top — they're your strongest keywords.
SUGGESTION: Remove outdated skills (e.g. Flash, Perl).

SECTION: Projects
SCORE: 8
VERDICT: Excellent
SUGGESTION: Link each project to a GitHub repo or live demo.
SUGGESTION: Add user or usage metrics where available.

SECTION: Education
SCORE: 7
VERDICT: Good
SUGGESTION: Add relevant coursework if < 3 years out of school.
SUGGESTION: Include GPA if 3.5+.

QUICK_WIN: Change headline to "Software Engineer | Python · SQL · AWS"
QUICK_WIN: Add one metric to your top experience bullet
QUICK_WIN: Request 10 endorsements for Python from former colleagues
"""


def test_parse_overall_score():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    assert report.overall_score == 62


def test_parse_overall_verdict():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    assert "quantification" in report.overall_verdict


def test_parse_sections_count():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    assert len(report.sections) == 6


def test_parse_section_names():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    names = [s.name for s in report.sections]
    assert "Headline" in names
    assert "Summary" in names
    assert "Experience" in names


def test_parse_section_scores():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    headline = next(s for s in report.sections if s.name == "Headline")
    assert headline.score == 7


def test_parse_section_verdicts():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    summary = next(s for s in report.sections if s.name == "Summary")
    assert summary.verdict == "Weak"


def test_parse_section_suggestions():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    headline = next(s for s in report.sections if s.name == "Headline")
    assert len(headline.suggestions) == 2
    assert "seniority" in headline.suggestions[1].lower()


def test_parse_quick_wins():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    assert len(report.quick_wins) == 3
    assert "headline" in report.quick_wins[0].lower()


def test_parse_raw_output_stored():
    report = _parse_audit_response(_SAMPLE_RESPONSE)
    assert "OVERALL_SCORE" in report.raw_llm_output


def test_parse_empty_response_defaults():
    report = _parse_audit_response("")
    assert report.overall_score == 50
    assert report.sections == []
    assert report.quick_wins == []


def test_parse_malformed_score_uses_default():
    raw = "OVERALL_SCORE: not-a-number\nOVERALL_VERDICT: ok"
    report = _parse_audit_response(raw)
    assert report.overall_score == 50  # default


# ---------------------------------------------------------------------------
# format_audit_report
# ---------------------------------------------------------------------------


def _make_report(score: int = 72) -> AuditReport:
    return AuditReport(
        overall_score=score,
        overall_verdict="Solid profile with room to grow.",
        sections=[
            AuditSection(
                name="Headline",
                score=8,
                verdict="Good",
                suggestions=["Add top skills to headline."],
            ),
            AuditSection(
                name="Summary",
                score=3,
                verdict="Weak",
                suggestions=["Open with a compelling story.", "Add metrics."],
            ),
        ],
        quick_wins=["Update headline", "Add one metric to top experience bullet"],
    )


def test_format_contains_score():
    report = _make_report(72)
    formatted = format_audit_report(report)
    assert "72/100" in formatted


def test_format_contains_verdict():
    report = _make_report()
    formatted = format_audit_report(report)
    assert "Solid profile" in formatted


def test_format_contains_section_names():
    report = _make_report()
    formatted = format_audit_report(report)
    assert "Headline" in formatted
    assert "Summary" in formatted


def test_format_contains_suggestions():
    report = _make_report()
    formatted = format_audit_report(report)
    assert "top skills" in formatted


def test_format_contains_quick_wins():
    report = _make_report()
    formatted = format_audit_report(report)
    assert "Quick wins" in formatted
    assert "Update headline" in formatted


def test_format_score_bar_length():
    """Bar should always be 10 chars of █/░."""
    report = _make_report(50)
    formatted = format_audit_report(report)
    # find the bar — it's between [ and ]
    import re
    match = re.search(r"\[([█░]+)\]", formatted)
    assert match, "Score bar not found"
    bar = match.group(1)
    assert len(bar) == 10


def test_format_emoji_in_section_lines():
    """Each section line should start with a colored circle or star emoji."""
    report = _make_report()
    formatted = format_audit_report(report)
    # At least some scoring emojis should appear
    assert any(ch in formatted for ch in ("🔴", "🟡", "🟢", "⭐"))
