from pathlib import Path

import pytest

from app.core.agent_runner import (
    AgentRunner,
    ApiKeyConflictError,
    _text_delta,
    ensure_subscription_auth,
)


def _runner(tmp_path: Path) -> AgentRunner:
    return AgentRunner(
        name="철수",
        model="sonnet",
        system_prompt="테스트",
        cwd=tmp_path / "agents" / "철수",
        allowed_tools=["Read", "Write"],
    )


def test_options_block_escape_tools_and_user_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    opts = _runner(tmp_path).build_options()
    assert "Bash" in opts.disallowed_tools
    assert "Bash" not in opts.tools
    assert opts.setting_sources == []
    # 사용자 계정의 claude.ai 커넥터(Gmail 등)가 에이전트에 붙지 않아야 한다
    assert opts.strict_mcp_config is True
    assert opts.mcp_servers == {}
    assert opts.include_partial_messages is True
    assert "ANTHROPIC_API_KEY" not in (opts.env or {})


def test_api_key_in_env_is_rejected(tmp_path, monkeypatch):
    # 키가 남아 있으면 종량제로 청구되므로 옵션 생성 단계에서 막는다.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    with pytest.raises(ApiKeyConflictError):
        _runner(tmp_path).build_options()


def test_ensure_subscription_auth_strips_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-dummy")
    removed = ensure_subscription_auth()
    assert set(removed) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    _runner(tmp_path).build_options()  # 이제 예외 없이 생성된다


def test_text_delta_parsing():
    ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "안녕"}}
    assert _text_delta(ev) == "안녕"
    assert _text_delta({"type": "message_start"}) is None
    assert (
        _text_delta({"type": "content_block_delta", "delta": {"type": "input_json_delta"}}) is None
    )
