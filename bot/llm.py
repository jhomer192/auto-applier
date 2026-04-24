import asyncio
import json
import subprocess

import yaml

from bot.models import JobAnalysis

GROUNDING_CONSTRAINT = (
    "\nCONSTRAINT: You are generating job application content for a real person.\n"
    "You MUST ONLY use facts explicitly stated in the PROFILE YAML below.\n"
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


class LLMError(Exception):
    pass


async def claude_call(prompt: str, max_tokens: int = 2000) -> str:
    """Run `claude -p <prompt>` in a thread to avoid blocking the event loop.

    Raises:
        LLMError: If the CLI exits non-zero or returns empty output.
    """
    def _run() -> str:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
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
    """Analyze a job posting against a candidate profile."""
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
        '  "tailored_summary": "<2-3 sentences from the profile that best match this role>"\n'
        "}\n\n"
        f"{GROUNDING_CONSTRAINT}"
    )
    raw = await claude_call(prompt)
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)
    return JobAnalysis(**data)


async def generate_field_answer(field_label: str, field_context: str, profile: dict) -> str:
    """Generate an answer for a single form field using the candidate profile.

    Returns:
        Answer string, or 'NEEDS_USER_INPUT:<field_label>' if profile is insufficient.
    """
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)
    prompt = (
        f"{GROUNDING_CONSTRAINT}\n\n"
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


async def generate_cover_letter(job_analysis: JobAnalysis, profile: dict) -> str:
    """Generate a grounded cover letter. Never invents facts."""
    safe_profile = _sanitize_profile(profile)
    profile_str = yaml.dump(safe_profile, default_flow_style=False)
    prompt = (
        f"{GROUNDING_CONSTRAINT}\n\n"
        f"PROFILE:\n{profile_str}\n\n"
        f"JOB: {job_analysis.title} at {job_analysis.company}\n"
        f"MATCH SUMMARY: {job_analysis.tailored_summary}\n\n"
        "Write a concise cover letter (3 paragraphs max) for this role.\n"
        "Only reference experience, skills, and facts explicitly in the profile.\n"
        "Do not mention anything not in the profile.\n\n"
        "Cover letter:"
    )
    return await claude_call(prompt)
