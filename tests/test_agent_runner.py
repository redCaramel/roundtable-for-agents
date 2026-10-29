from pathlib import Path

import pytest

from app.core.agent_runner import (
    AgentRunner,
    ApiKeyConflictError,
    _text_delta,
    ensure_subscription_auth,
)
from app.core.sandbox import SANDBOX_TOOLS, Sandbox


def _runner(tmp_path: Path) -> AgentRunner:
    own = tmp_path / "agents" / "철수"
    own.mkdir(parents=True, exist_ok=True)
    return AgentRunner(
        name="철수",
        model="sonnet",
        system_prompt="테스트",
        cwd=own,
        tools=list(SANDBOX_TOOLS),
        sandbox=Sandbox(own, protected_root=tmp_path),
    )


def _no_keys(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def test_options_block_escape_tools_and_user_settings(tmp_path, monkeypatch):
    _no_keys(monkeypatch)
    opts = _runner(tmp_path).build_options()
    assert "Bash" in opts.disallowed_tools
    assert "Bash" not in opts.tools
    assert opts.setting_sources == []
    # 사용자 계정의 claude.ai 커넥터(Gmail 등)가 에이전트에 붙지 않아야 한다
    assert opts.strict_mcp_config is True
    assert opts.mcp_servers == {}
    assert opts.include_partial_messages is True
    assert "ANTHROPIC_API_KEY" not in (opts.env or {})


def test_no_tool_is_auto_approved_and_hook_gates_all(tmp_path, monkeypatch):
    _no_keys(monkeypatch)
    opts = _runner(tmp_path).build_options()
    # allowed_tools에 들어가면 샌드박스 판정 없이 자동 허용되므로 비어 있어야 한다
    assert opts.allowed_tools == []
    matchers = opts.hooks["PreToolUse"]
    assert len(matchers) == 1 and matchers[0].matcher is None  # 모든 도구에 적용
    assert opts.can_use_tool is not None


def test_tools_require_sandbox(tmp_path):
    with pytest.raises(ValueError):
        AgentRunner(name="a", model="haiku", system_prompt="", cwd=tmp_path, tools=["Read"])


async def _hook(runner: AgentRunner, tool: str, tool_input: dict) -> dict:
    out = await runner._pre_tool_use({"tool_name": tool, "tool_input": tool_input}, "id", None)
    return out["hookSpecificOutput"]


async def test_pre_tool_use_hook_decisions_and_activity(tmp_path):
    runner = _runner(tmp_path)
    seen: list[tuple[str, bool]] = []
    runner._on_tool = lambda tool, inp, d: seen.append((tool, d.allowed))

    allow = await _hook(runner, "Write", {"file_path": "memo.md", "content": "x"})
    assert allow["permissionDecision"] == "allow"
    deny = await _hook(runner, "Read", {"file_path": "../../secret.txt"})
    assert deny["permissionDecision"] == "deny"
    assert "권한" in deny["permissionDecisionReason"]
    bash = await _hook(runner, "Bash", {"command": "dir"})
    assert bash["permissionDecision"] == "deny"
    assert seen == [("Write", True), ("Read", False), ("Bash", False)]


async def test_hook_denies_everything_without_sandbox(tmp_path):
    runner = AgentRunner(name="a", model="haiku", system_prompt="", cwd=tmp_path)
    out = await _hook(runner, "Read", {"file_path": "x"})
    assert out["permissionDecision"] == "deny"


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


def test_custom_tools_exposed_as_roundtable_server(tmp_path, monkeypatch):
    from app.core.access_control import AccessControl
    from app.core.tools import build_access_tools

    _no_keys(monkeypatch)
    own = tmp_path / "agents" / "철수"
    own.mkdir(parents=True)
    acl = AccessControl(["철수", "영희"])
    runner = AgentRunner(
        name="철수",
        model="haiku",
        system_prompt="",
        cwd=own,
        tools=list(SANDBOX_TOOLS),
        sandbox=Sandbox(own),
        custom_tools=build_access_tools(acl, "철수", lambda: 0, folder_of=str),
    )
    opts = runner.build_options()
    assert list(opts.mcp_servers) == ["roundtable"]
    assert opts.strict_mcp_config is True


async def test_cancelled_turn_reconnects_with_resume(tmp_path, monkeypatch):
    import asyncio

    from app.core import agent_runner as ar

    _no_keys(monkeypatch)
    created: list = []

    class FakeClient:
        def __init__(self, options):
            self.options = options
            self.disconnected = False
            created.append(self)

        async def connect(self):
            pass

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(ar, "ClaudeSDKClient", FakeClient)
    runner = _runner(tmp_path)
    runner.session_id = "sess-1"

    async def cancelled_turn(prompt, on_text):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_run_turn", cancelled_turn)
    with pytest.raises(asyncio.CancelledError):
        await runner.run_turn("안녕")
    assert len(created) == 1

    # 응답을 다 읽지 못한 연결은 버리고, 같은 세션을 resume으로 새로 연다
    await runner.connect()
    assert created[0].disconnected
    assert len(created) == 2 and created[1].options.resume == "sess-1"
    await runner.connect()
    assert len(created) == 2  # 정상 상태면 다시 열지 않는다
