import asyncio
import json
import subprocess

import yaml

from bot.models import JobAnalysis

GROUNDING_CONSTRAINT = (
    "\nCONSTRAINT: You are generating job application content for a real person.\n"
    "You MUST ONLY use facts explicitly stated in the PROFILE YAML below.\n"
    "You may rephrase and reorder the candidate's real experience to best match the role,\n"
    "but you may NOT add facts not in the profile.\n"
    "Do NOT invent, infer, embellish, or add any fact not stated in the profile.\n"
    "If a required field cannot be answered from the profile, respond with exactly:\n"
    "  NEEDS_USER_INPUT:<field_label>\n"
)

# Sentinel prefix — sanitize profile values that accidentally contain it
_SENTINEL_PREFIX = "NEEDS_USER_INPUT"
_CONSTRAINT_MARKER = "CONSTRAINT:"


def _sanitize_profile(profile: dict) -> dict:
    """Return a copy of profile with string values stripped of leading/trailing
    whitespace and any value that starts with our sentinel or constraint marker
    replaced with a safe placeholder.  This prevents a crafted profile.yaml
    from injecting instructions into the LLM prompt.
    """
    def _clean(v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith(_SENTINEL_PREFIX) or v.startswith(_CONSTRAINT_MARKER):
                return "[REDACTED — invalid profile value]"
            return v
        if isinstance(v, dict):
            return {k: _clean(val) for k, val in v.items()}
        if isinstance(v, list):
            return [_clean(item) for item in v]
        return v

    return {k: _clean(val) for k, val in profile.items()}


def _build_tailoring_context(job_analysis: JobAnalysis) -> str:
    """Format a JobAnalysis into a compact context block for use in prompts.

    Args:
        job_analysis: Analyzed job posting with extracted signals.

    Returns:
        A formatted string to prepend to field answer and cover letter prompts.
    """
    required = ", ".join(job_analysis.required_skills) if job_analysis.required_skills else "none listed"
    preferred = ", ".join(job_analysis.preferred_skills) if job_analysis.preferred_skills else "none listed"
    responsibilities = (
        "\n".join(f"  - {r}" for r in job_analysis.key_responsibilities)
        if job_analysis.key_responsibilities
        else "  - (not extracted)"
    )
    ats_keywords = ", ".join(job_analysis.ats_keywords) if job_analysis.ats_keywords else "none"

    return (
        f"JOB TAILORING CONTEXT:\n"
        f"Role: {job_analysis.title} at {job_analysis.company}\n"
        f"Company tone: {job_analysis.company_tone}\n"
        f"Required skills: {required}\n"
        f"Preferred skills: {preferred}\n"
        f"Key responsibilities:\n{responsibilities}\n"
        f"ATS keywords (use these exact phrases where they match your experience): {ats_keywords}\n"
        f"What makes this role distinctive: {job_analysis.why_this_role}\n"
        f"Tailoring instruction: Mirror the company's {job_analysis.company_tone} tone. "
        f"Prioritize the required skills and ATS keywords above. "
        f"Choose profile facts that directly address the key responsibilities listed.\n"
    )


class LLMError(Exception):
    pass


async def claude_call(prompt: str, max_tokens: int = 2000) -> str:
    """Run `claude -p <prompt>` in a thread to avoid blocking the event loop.

    Raises:
        LLMError: If the CLI exits non-zero or returns empty output.
    """
    def _run() -> str:
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise LLMError("claude CLI not found — install it: npm install -g @anthropic-ai/claude-code")
        if result.returncode != 0:
            raise LLMError(
                f"claude CLI failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        output = result.stdout.strip()
        if not output:
            raise LLMError("claude CLI returned empty output")
        return output

    return await asyncio.to_thread(_run)


async def analyze_job(job_html: str, profile: dict) -> JobAnalysis:
    """Analyze a job posting against a candidate profile.

    Extracts structured signals: skills, responsibilities, tone, ATS keywords, salary,
    seniority level, work arrangement, and a normalised role type — all in one LLM call.

    Args:
        job_html: Raw HTML of the job posting (truncated to 8000 chars internally).
        profile: Candidate profile dict loaded from profile.yaml.

    Returns:
        JobAnalysis populated with all extracted fields.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)
    prompt = (
        "Analyze this job posting and the candidate's profile.\n\n"
        f"JOB HTML (first 8000 chars):\n{job_html[:8000]}\n\n"
        f"PROFILE:\n{profile_str}\n\n"
        "Respond in JSON with these exact keys:\n"
        '{\n'
        '  "title": "<job title>",\n'
        '  "company": "<company name>",\n'
        '  "match_score": <0-100 integer>,\n'
        '  "tailored_summary": "<2-3 sentences from the profile that best match this role>",\n'
        '  "required_skills": ["<skill>", ...],\n'
        '  "preferred_skills": ["<skill>", ...],\n'
        '  "key_responsibilities": ["<responsibility>", ...],\n'
        '  "company_tone": "<one of: formal | casual | mission-driven | technical>",\n'
        '  "ats_keywords": ["<exact phrase from JD>", ...],\n'
        '  "why_this_role": "<one sentence: what makes this specific role distinctive>",\n'
        '  "salary_min": <annual USD integer, 0 if not stated>,\n'
        '  "salary_max": <annual USD integer, 0 if not stated>,\n'
        '  "salary_currency": "<e.g. USD>",\n'
        '  "salary_is_estimated": <true if you estimated, false if explicitly posted>,\n'
        '  "seniority_level": "<one of: junior | mid | senior | staff | principal | director | unknown>",\n'
        '  "work_arrangement": "<one of: remote | hybrid | onsite | unknown>",\n'
        '  "role_type": "<short normalised role category, e.g. software engineer | data scientist>",\n'
        '  "sponsors_visa": <true if posting says they sponsor visas/H-1B; false if posting says '
        '"must be authorized to work" or "no sponsorship"; null if not mentioned>\n'
        "}\n\n"
        "Guidelines:\n"
        "- required_skills: explicitly required (up to 10)\n"
        "- preferred_skills: nice-to-have (up to 8)\n"
        "- key_responsibilities: main day-to-day duties (up to 6)\n"
        "- company_tone: single best descriptor for the JD's writing style\n"
        "- ats_keywords: exact noun phrases ATS systems scan for (up to 15)\n"
        "- salary_min/salary_max: convert hourly/monthly to annual if needed;\n"
        "  if the JD does not state salary, estimate based on role/level/location/company size\n"
        "  and set salary_is_estimated=true\n"
        "- seniority_level: infer from title keywords (Senior/Staff/Principal/etc.) or JD text\n"
        "- work_arrangement: look for Remote/Hybrid/In-office/On-site language\n"
        "- role_type: normalised lowercase category (ignore seniority prefix)\n\n"
        f"{GROUNDING_CONSTRAINT}"
    )
    raw = await claude_call(prompt)
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)
    # Pop unknown keys so JobAnalysis(**data) doesn't break on old/extra fields
    known = {f.name for f in JobAnalysis.__dataclass_fields__.values()} if hasattr(JobAnalysis, '__dataclass_fields__') else set()
    if known:
        data = {k: v for k, v in data.items() if k in known}
    return JobAnalysis(**data)


async def generate_field_answer(
    field_label: str,
    field_context: str,
    profile: dict,
    job_analysis: JobAnalysis | None = None,
    field_hint: str | None = None,
) -> str:
    """Generate an answer for a single form field using the candidate profile.

    When job_analysis is provided, answers are tailored to emphasize skills and
    experience most relevant to that specific role, using the job's language and tone.

    Args:
        field_label: The visible label of the form field.
        field_context: Surrounding form text for additional context.
        profile: Candidate profile dict.
        job_analysis: Optional analyzed job posting for tailoring. Defaults to None.
        field_hint: Optional type-specific instruction (e.g., valid dropdown options,
            EEO guidance) from the scraper. Prepended to the prompt if provided.

    Returns:
        Answer string, or 'NEEDS_USER_INPUT:<field_label>' if profile is insufficient.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)

    tailoring_block = ""
    if job_analysis is not None:
        tailoring_block = (
            f"\n{_build_tailoring_context(job_analysis)}\n"
            "Use the exact ATS keywords from the job where they match your experience. "
            "Adjust tone to match the company's voice. "
            "Choose which profile facts to highlight based on the key responsibilities above.\n"
        )

    hint_block = f"\nFIELD GUIDANCE: {field_hint}\n" if field_hint else ""

    prompt = (
        f"{GROUNDING_CONSTRAINT}\n"
        f"{tailoring_block}"
        f"{hint_block}\n"
        f"PROFILE:\n{profile_str}\n\n"
        f"FORM FIELD: {field_label}\n"
        f"CONTEXT (surrounding form text): {field_context}\n\n"
        "Provide the best answer for this field using only information from the profile above.\n"
        'If this is a yes/no question, answer with just "Yes" or "No".\n'
        "If this is an open text field, keep it concise (1-3 sentences max).\n"
        f"If the profile contains no relevant information, respond: NEEDS_USER_INPUT:{field_label}\n\n"
        "Answer:"
    )
    return await claude_call(prompt)


def _build_academic_block(profile: dict) -> str:
    """Build an academic background context string for LLM prompts.

    Args:
        profile: Candidate profile dict (already sanitized).

    Returns:
        A formatted string with research-to-industry bridging instructions,
        or an empty string when no academic section is present.
    """
    acad = profile.get("academic", {})
    if not acad:
        return ""
    areas = ", ".join(acad.get("research_areas", []))
    thesis = acad.get("thesis", "")
    pubs: list[str] = acad.get("publications", [])
    return (
        f"\nCANDIDATE BACKGROUND: Grad student / recent grad from "
        f"{acad.get('university', 'university')}, {acad.get('degree', 'grad')} in "
        f"{acad.get('department', 'technical field')}. "
        + (f"Research: {areas}. " if areas else "")
        + (f"Thesis: {thesis}. " if thesis else "")
        + (f"Publications: {'; '.join(pubs[:2])}. " if pubs else "")
        + "\nIMPORTANT: Explicitly bridge this candidate's research background to "
        "the industry role. Show how research skills translate to the company's "
        "problems. Connect specific research work to specific job responsibilities. "
        "Don't just list skills.\n"
    )


async def tailor_resume(job_analysis: JobAnalysis, profile: dict) -> str:
    """Generate a tailored resume in plain Markdown for a specific role.

    Reorders and rephrases the candidate's real experience to best match the job,
    emphasizing relevant skills and using ATS keywords. Never invents facts.
    When the profile includes an academic section, research experience is explicitly
    bridged to the industry role.

    Args:
        job_analysis: Analyzed job posting.
        profile: Candidate profile dict.

    Returns:
        Markdown-formatted resume string tailored to this role.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)
    tailoring_context = _build_tailoring_context(job_analysis)
    academic_block = _build_academic_block(safe_profile)

    prompt = (
        f"{GROUNDING_CONSTRAINT}\n\n"
        f"{tailoring_context}\n"
        f"{academic_block}"
        f"PROFILE:\n{profile_str}\n\n"
        "Generate a tailored resume in Markdown for this specific role. Follow these rules:\n"
        "1. Start with the candidate's name and contact info (email, phone, location, LinkedIn/GitHub if present).\n"
        "2. Write a 2-3 sentence Summary that connects the candidate's top experience to the role's key responsibilities.\n"
        "   Use at least 2 ATS keywords. Match the company's tone.\n"
        "3. Skills section: list only skills that appear in the profile AND are relevant to this role.\n"
        "   Prioritize required_skills and preferred_skills from the job analysis.\n"
        "4. Experience section: for each role in work_history, write 2-4 bullet points.\n"
        "   Lead each bullet with an action verb. Include metrics where the profile states them.\n"
        "   Emphasize bullets most relevant to this role's responsibilities.\n"
        "5. Education section: include degree, institution, graduation year.\n"
        "6. Only use facts from the profile. Do NOT invent metrics, titles, or responsibilities.\n"
        "7. Keep total length under 700 words.\n\n"
        "Tailored resume (Markdown):"
    )
    return await claude_call(prompt, max_tokens=1200)


async def extract_achievements(answers: list[tuple[str, str]], profile: dict) -> str:
    """Extract structured YAML achievement bullets from a profile interview.

    Takes a list of (question, answer) pairs from the profile-building conversation
    and returns new YAML entries ready to append to profile.yaml.

    Args:
        answers: List of (question, answer) tuples from the interview.
        profile: Current profile dict (to avoid duplicating existing content).

    Returns:
        A YAML string with new `achievements` entries to merge into the profile.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)

    qa_block = "\n".join(
        f"Q: {q}\nA: {a}" for q, a in answers
    )

    prompt = (
        f"{GROUNDING_CONSTRAINT}\n\n"
        "Below is a profile-building interview. Extract concrete achievements and add them "
        "to the candidate's profile.\n\n"
        f"CURRENT PROFILE:\n{profile_str}\n\n"
        f"INTERVIEW:\n{qa_block}\n\n"
        "Extract achievements from the interview answers. Output ONLY valid YAML with this structure:\n"
        "achievements:\n"
        "  - summary: <one-sentence achievement>\n"
        "    impact: <quantified impact or qualitative outcome>\n"
        "    skills: [<skill1>, <skill2>]\n"
        "    context: <company or project name if mentioned>\n\n"
        "Rules:\n"
        "- Only extract facts explicitly stated in the interview answers.\n"
        "- Do NOT duplicate achievements already in the profile.\n"
        "- If no new achievements can be extracted, output: achievements: []\n"
        "- Minimum 1 sentence, maximum 3 sentences per summary.\n"
        "- Output ONLY the YAML block, no explanation.\n\n"
        "YAML:"
    )
    return await claude_call(prompt, max_tokens=800)


async def generate_cover_letter(job_analysis: JobAnalysis, profile: dict) -> str:
    """Generate a grounded, tailored cover letter. Never invents facts.

    The letter is structured to open by connecting a specific candidate experience
    to a specific job responsibility, weave in ATS keywords naturally, and close
    by referencing what makes this particular role distinctive. When the profile
    includes an academic section, research experience is explicitly bridged to the
    industry role.

    Args:
        job_analysis: Expanded analysis of the job posting including tone and keywords.
        profile: Candidate profile dict.

    Returns:
        A cover letter string of at most 3 paragraphs.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)
    tailoring_context = _build_tailoring_context(job_analysis)
    academic_block = _build_academic_block(safe_profile)

    prompt = (
        f"{GROUNDING_CONSTRAINT}\n\n"
        f"{tailoring_context}\n"
        f"{academic_block}"
        f"PROFILE:\n{profile_str}\n\n"
        "Write a cover letter for this role following these exact rules:\n"
        "1. Exactly 3 paragraphs — no more, no less.\n"
        "2. Opening paragraph: connect ONE specific piece of the candidate's experience directly\n"
        "   to ONE specific key responsibility listed above. Be concrete, not generic.\n"
        "3. Middle paragraph: demonstrate fit using at least 3 of the ATS keywords listed above,\n"
        "   woven in naturally — not forced. Only use keywords where the profile actually supports them.\n"
        "4. Closing paragraph: reference why_this_role specifically to show genuine interest in\n"
        "   THIS role, not just any job. End with a clear call to action.\n"
        f"5. Tone throughout must match: {job_analysis.company_tone}.\n"
        "6. Only reference experience, skills, and facts explicitly in the profile.\n"
        "   Do not mention anything not in the profile.\n\n"
        "Cover letter:"
    )
    return await claude_call(prompt)
