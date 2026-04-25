import subprocess
import pytest
from unittest.mock import patch, MagicMock, call
import asyncio
from bot.llm import claude_call, LLMError, GROUNDING_CONSTRAINT, _extract_json


def run(coro):
    return asyncio.run(coro)


def mock_subprocess(returncode=0, stdout="test output", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_claude_call_returns_stdout():
    with patch("subprocess.run", return_value=mock_subprocess(stdout="hello world")):
        result = run(claude_call("test prompt"))
    assert result == "hello world"


def test_claude_call_raises_on_nonzero_exit():
    with patch("subprocess.run", return_value=mock_subprocess(returncode=1, stderr="error")):
        with pytest.raises(LLMError, match="exit 1"):
            run(claude_call("test"))


def test_claude_call_raises_on_empty_stdout():
    with patch("subprocess.run", return_value=mock_subprocess(stdout="")):
        with pytest.raises(LLMError, match="empty output"):
            run(claude_call("test"))


def test_claude_call_strips_whitespace():
    with patch("subprocess.run", return_value=mock_subprocess(stdout="  trimmed  \n")):
        result = run(claude_call("test"))
    assert result == "trimmed"


def test_grounding_constraint_present_in_module():
    """GROUNDING_CONSTRAINT must mention NEEDS_USER_INPUT -- this is the safety mechanism."""
    assert "NEEDS_USER_INPUT" in GROUNDING_CONSTRAINT


def test_generate_field_answer_passes_constraint(valid_profile):
    """The prompt sent to claude must contain the grounding constraint."""
    _, profile = valid_profile
    captured_prompt = []

    def fake_run(args, **kwargs):
        captured_prompt.append(args[-1])  # last arg is the prompt
        return mock_subprocess(stdout="5 years")

    with patch("subprocess.run", side_effect=fake_run):
        from bot.llm import generate_field_answer
        run(generate_field_answer("Years of experience", "Engineering role", profile))

    assert captured_prompt, "subprocess.run was not called"
    assert "NEEDS_USER_INPUT" in captured_prompt[0]


def test_generate_field_answer_returns_needs_user_input_passthrough(valid_profile):
    """If LLM responds NEEDS_USER_INPUT:X, it must be returned as-is."""
    _, profile = valid_profile
    with patch("subprocess.run", return_value=mock_subprocess(stdout="NEEDS_USER_INPUT:Cover Letter")):
        from bot.llm import generate_field_answer
        result = run(generate_field_answer("Cover Letter", "", profile))
    assert result.startswith("NEEDS_USER_INPUT:")


# ── _extract_json ─────────────────────────────────────────────────────────────

def test_extract_json_plain_object():
    raw = '{"key": "value"}'
    assert _extract_json(raw) == '{"key": "value"}'


def test_extract_json_plain_array():
    raw = '[1, 2, 3]'
    assert _extract_json(raw) == '[1, 2, 3]'


def test_extract_json_backtick_json_fence():
    raw = '```json\n{"key": "value"}\n```'
    assert _extract_json(raw) == '{"key": "value"}'


def test_extract_json_backtick_fence_no_lang():
    raw = '```\n{"key": "value"}\n```'
    assert _extract_json(raw) == '{"key": "value"}'


def test_extract_json_backtick_fence_uppercase():
    raw = '```JSON\n{"key": 1}\n```'
    assert _extract_json(raw) == '{"key": 1}'


def test_extract_json_embedded_in_prose():
    raw = 'Here is the JSON:\n\n{"score": 82, "title": "Engineer"}\n\nThat is all.'
    result = _extract_json(raw)
    import json
    parsed = json.loads(result)
    assert parsed["score"] == 82


def test_extract_json_whitespace_around_fence():
    raw = '\n\n```json\n{"a": 1}\n```\n\n'
    result = _extract_json(raw)
    assert result == '{"a": 1}'


def test_extract_json_returns_parseable_json_for_all_claude_patterns():
    """All common Claude output patterns must produce parseable JSON."""
    import json
    patterns = [
        '{"x": 1}',
        '```json\n{"x": 1}\n```',
        '```\n{"x": 1}\n```',
        'Sure! Here:\n```json\n{"x": 1}\n```\nLet me know.',
        'Result: {"x": 1}',
    ]
    for raw in patterns:
        result = _extract_json(raw)
        parsed = json.loads(result)
        assert parsed["x"] == 1, f"Failed to parse pattern: {raw!r}"


# ── retry logic ───────────────────────────────────────────────────────────────

def test_claude_call_retries_on_timeout():
    """Should retry up to 3 times on timeout, then raise."""
    import subprocess as sp
    side_effects = [
        sp.TimeoutExpired(["claude"], 120),
        sp.TimeoutExpired(["claude"], 120),
        sp.TimeoutExpired(["claude"], 120),
    ]
    with patch("subprocess.run", side_effect=side_effects):
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMError, match="timed out"):
                run(claude_call("test"))


def test_claude_call_succeeds_after_retry():
    """Should succeed on second attempt after a transient timeout."""
    import subprocess as sp
    side_effects = [
        sp.TimeoutExpired(["claude"], 120),
        mock_subprocess(stdout="success on retry"),
    ]
    with patch("subprocess.run", side_effect=side_effects):
        with patch("asyncio.sleep", return_value=None):
            result = run(claude_call("test"))
    assert result == "success on retry"


def test_claude_call_not_found_is_permanent():
    """FileNotFoundError should NOT be retried — it's a permanent config failure."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(LLMError, match="not found"):
            run(claude_call("test"))
