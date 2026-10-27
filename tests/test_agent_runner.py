from pathlib import Path

import pytest

from app.core.agent_runner import AgentRunner, MissingApiKeyError, _text_delta


def _runner(tmp_path: Path) -> AgentRunner:
    return AgentRunner(
        name="철수",
        model="sonnet",
        system_prompt="테스트",
        cwd=tmp_path / "agents" / "철수",
        allowed_tools=["Read", "Write"],
    )


def test_options_block_escape_tools_and_user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    opts = _runner(tmp_path).build_options()
    assert "Bash" in opts.disallowed_tools
    assert "Bash" not in opts.tools
    assert opts.setting_sources == []
    assert opts.include_partial_messages is True
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-test-dummy"


def test_missing_api_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        _runner(tmp_path).build_options()


def test_text_delta_parsing():
    ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "안녕"}}
    assert _text_delta(ev) == "안녕"
    assert _text_delta({"type": "message_start"}) is None
    assert (
        _text_delta({"type": "content_block_delta", "delta": {"type": "input_json_delta"}}) is None
    )
