"""LinkedIn profile auditor.

Fetches the user's own LinkedIn profile using the stored auth state and runs an
LLM audit against their profile.yaml. Returns a structured AuditReport with a
score and actionable recommendations for each section.

Usage:
    report = await audit_linkedin_profile(profile_url, profile_yaml, auth_state)
    for section in report.sections:
        print(section.name, section.score, section.suggestions)
"""
import logging
import re
from dataclasses import dataclass, field

from bot.llm import claude_call, LLMError
from bot.human import launch_stealth_context, page_load_pause, read_pause, human_scroll

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class AuditSection:
    name: str           # e.g. "Headline", "Summary", "Experience"
    score: int          # 0-10
    verdict: str        # one-line grade ("Strong", "Weak", "Missing", etc.)
    suggestions: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    overall_score: int          # 0-100
    overall_verdict: str
    sections: list[AuditSection] = field(default_factory=list)
    quick_wins: list[str] = field(default_factory=list)     # top 3 highest-impact changes
    raw_llm_output: str = ""


# ---------------------------------------------------------------------------
# LinkedIn profile scraper
# ---------------------------------------------------------------------------


async def fetch_linkedin_profile_html(profile_url: str, auth_state_path: str) -> str:
    """Fetch a LinkedIn profile page and return its raw HTML.

    Uses the stored auth state so the full profile is visible (not the
    gated view shown to logged-out visitors).

    Args:
        profile_url: Full URL to the LinkedIn profile (linkedin.com/in/...).
        auth_state_path: Path to the Playwright storage state JSON file.

    Returns:
        Raw HTML of the profile page.

    Raises:
        RuntimeError: If the page cannot be loaded or returns an error.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed — run: pip install playwright")

    async with async_playwright() as p:
        browser, ctx = await launch_stealth_context(p, auth_state_path)
        page = await ctx.new_page()
        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page_load_pause()
            await human_scroll(page)
            await read_pause(500)
            # Scroll a second time to trigger lazy-loaded sections
            await human_scroll(page)
            await read_pause(300)
            html = await page.content()
            return html
        except Exception as e:
            raise RuntimeError(f"Could not load LinkedIn profile: {e}") from e
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# HTML → text extraction
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace. Not a full parser — good enough
    for extracting readable LinkedIn profile text for an LLM prompt."""
    # Remove <script> and <style> blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace common structural tags with newlines
    html = re.sub(r"<(br|p|div|li|h[1-6]|section)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Remove remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    # Collapse whitespace
    lines = [line.strip() for line in html.splitlines()]
    lines = [l for l in lines if l]
    # Deduplicate consecutive identical lines (LinkedIn repeats a lot)
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return "\n".join(deduped)


def _truncate_profile_text(text: str, max_chars: int = 6000) -> str:
    """Truncate extracted profile text to fit within LLM prompt limits."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


# ---------------------------------------------------------------------------
# LLM audit
# ---------------------------------------------------------------------------


_AUDIT_SYSTEM = """\
You are a senior technical recruiter and LinkedIn profile coach. You will audit
a LinkedIn profile and provide specific, actionable feedback.

Evaluate these six sections and score each 0-10:
  1. Headline      — keywords, role clarity, differentiation
  2. Summary/About — story arc, metrics, call-to-action
  3. Experience    — STAR format, quantified achievements, keyword density
  4. Skills        — top-skill relevance, endorsements gap, ordering
  5. Projects      — visibility, impact metrics, tech stack clarity
  6. Education     — completeness, relevant coursework, honors

IMPORTANT SCORING GUIDELINES:
- A profile with no summary = 0 for Summary
- Experience descriptions that are just job duties (no metrics/outcomes) = 3-4
- Quantified experience (numbers, percentages, scale) = 7-9
- Be harsh but fair — most LinkedIn profiles score 4-6 overall

Respond ONLY in this exact format (no prose before or after):
OVERALL_SCORE: <0-100>
OVERALL_VERDICT: <one sentence>

SECTION: Headline
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

SECTION: Summary
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

SECTION: Experience
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

SECTION: Skills
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

SECTION: Projects
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

SECTION: Education
SCORE: <0-10>
VERDICT: <Excellent|Good|Weak|Missing>
SUGGESTION: <specific actionable fix>
SUGGESTION: <specific actionable fix>

QUICK_WIN: <highest-impact single change you could make in 5 minutes>
QUICK_WIN: <second highest-impact change>
QUICK_WIN: <third highest-impact change>
"""


async def audit_profile_with_llm(
    profile_text: str,
    profile_yaml: dict,
    target_roles: list[str] | None = None,
) -> AuditReport:
    """Run an LLM audit on extracted LinkedIn profile text.

    Args:
        profile_text: Plain text extracted from the LinkedIn profile page.
        profile_yaml: The user's local profile.yaml dict (for cross-referencing).
        target_roles: Optional list of roles the user is targeting, for tailored feedback.

    Returns:
        Parsed AuditReport.

    Raises:
        LLMError: If the Claude call fails.
    """
    import yaml as yaml_mod

    # Build role context
    role_context = ""
    if target_roles:
        role_context = f"\nTARGET ROLES: {', '.join(target_roles)}\n"

    # Summarize the local profile for cross-reference
    local_summary_parts = []
    if profile_yaml.get("work_history"):
        local_summary_parts.append(f"Work history entries: {len(profile_yaml['work_history'])}")
    if profile_yaml.get("projects"):
        local_summary_parts.append(f"Projects in local profile: {len(profile_yaml['projects'])}")
    if profile_yaml.get("certifications"):
        local_summary_parts.append(f"Certifications: {len(profile_yaml['certifications'])}")
    if profile_yaml.get("competitions"):
        local_summary_parts.append(f"Awards/Competitions: {len(profile_yaml['competitions'])}")
    local_context = "\nLOCAL PROFILE STATS: " + "; ".join(local_summary_parts) if local_summary_parts else ""

    prompt = (
        f"{_AUDIT_SYSTEM}\n\n"
        f"LINKEDIN PROFILE TEXT:\n"
        f"---\n"
        f"{profile_text}\n"
        f"---"
        f"{role_context}"
        f"{local_context}\n\n"
        "Now produce the structured audit in the exact format above."
    )

    raw = await claude_call(prompt, max_tokens=1200)
    return _parse_audit_response(raw)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_audit_response(raw: str) -> AuditReport:
    """Parse the structured LLM audit response into an AuditReport."""
    lines = raw.strip().splitlines()

    overall_score = 50
    overall_verdict = ""
    sections: list[AuditSection] = []
    quick_wins: list[str] = []

    current_section: AuditSection | None = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("OVERALL_SCORE:"):
            try:
                overall_score = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

        elif line.startswith("OVERALL_VERDICT:"):
            overall_verdict = line.split(":", 1)[1].strip()

        elif line.startswith("SECTION:"):
            if current_section:
                sections.append(current_section)
            name = line.split(":", 1)[1].strip()
            current_section = AuditSection(name=name, score=5, verdict="Unknown")

        elif line.startswith("SCORE:") and current_section:
            try:
                current_section.score = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

        elif line.startswith("VERDICT:") and current_section:
            current_section.verdict = line.split(":", 1)[1].strip()

        elif line.startswith("SUGGESTION:") and current_section:
            suggestion = line.split(":", 1)[1].strip()
            if suggestion:
                current_section.suggestions.append(suggestion)

        elif line.startswith("QUICK_WIN:"):
            win = line.split(":", 1)[1].strip()
            if win:
                quick_wins.append(win)

    if current_section:
        sections.append(current_section)

    return AuditReport(
        overall_score=overall_score,
        overall_verdict=overall_verdict,
        sections=sections,
        quick_wins=quick_wins,
        raw_llm_output=raw,
    )


# ---------------------------------------------------------------------------
# Formatting for Telegram
# ---------------------------------------------------------------------------


_SCORE_EMOJI = {
    range(0, 4): "🔴",
    range(4, 7): "🟡",
    range(7, 9): "🟢",
    range(9, 11): "⭐",
}


def _score_emoji(score: int, out_of: int = 10) -> str:
    normalized = int(score * 10 / out_of)
    for r, emoji in _SCORE_EMOJI.items():
        if normalized in r:
            return emoji
    return "⚪"


def format_audit_report(report: AuditReport) -> str:
    """Format an AuditReport into a readable Telegram message."""
    bar_filled = int(report.overall_score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    lines = [
        f"*LinkedIn Profile Audit*",
        f"Score: *{report.overall_score}/100* [{bar}]",
        f"{report.overall_verdict}",
        "",
    ]

    for section in report.sections:
        emoji = _score_emoji(section.score)
        lines.append(f"{emoji} *{section.name}* — {section.score}/10 ({section.verdict})")
        for s in section.suggestions[:2]:  # cap at 2 per section for readability
            lines.append(f"  • {s}")

    if report.quick_wins:
        lines.append("")
        lines.append("*Quick wins (do these first):*")
        for win in report.quick_wins[:3]:
            lines.append(f"  🎯 {win}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


async def run_linkedin_audit(
    profile_url: str,
    profile: dict,
    auth_state_path: str,
) -> AuditReport:
    """End-to-end LinkedIn audit: fetch → extract → audit → return report.

    Args:
        profile_url: The user's LinkedIn profile URL.
        profile: Their local profile.yaml dict.
        auth_state_path: Path to LinkedIn auth state JSON.

    Returns:
        Filled AuditReport ready to format and send.
    """
    from bot.profile import load_preferences
    prefs = load_preferences(profile)
    target_roles = prefs.desired_roles or []

    html = await fetch_linkedin_profile_html(profile_url, auth_state_path)
    text = _strip_html(html)
    text = _truncate_profile_text(text)

    logger.info("linkedin audit: extracted %d chars of profile text", len(text))

    return await audit_profile_with_llm(text, profile, target_roles=target_roles)
