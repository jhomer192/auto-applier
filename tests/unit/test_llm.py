import subprocess
import pytest
from unittest.mock import patch, MagicMock
import asyncio
from bot.llm import claude_call, LLMError, GROUNDING_CONSTRAINT


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
