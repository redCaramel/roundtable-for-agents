"""M8: 오류 안내와 토큰 상한 자동 일시정지."""

import pytest
from claude_agent_sdk import CLINotFoundError

from app.core.errors import friendly_error
from app.core.models import AgentConfig, ConversationState, SessionConfig
from app.core.orchestrator import Orchestrator
from tests.fakes import FakeAgent


@pytest.mark.parametrize(
    "error,expected",
    [
        (CLINotFoundError("Claude Code not found"), "다시 설치"),
        (RuntimeError("Invalid API key · Please run /login"), "/login"),
        ("Not logged in", "/login"),
        ("API Error: 429 rate_limit_error", "사용 한도"),
        ("Claude AI usage limit reached|1761800000", "사용 한도"),
        ("API Error: 529 overloaded_error", "혼잡"),
        (ConnectionError("Connection reset by peer"), "네트워크"),
        (TimeoutError("timed out"), "네트워크"),
    ],
)
def test_friendly_error_maps_known_causes(error, expected):
    assert expected in friendly_error(error)


def test_friendly_error_keeps_unknown_detail_short():
    msg = friendly_error(ValueError("x" * 500))
    assert msg.startswith("ValueError: xxx") and "▶ 재개" in msg
    assert len(msg) < 220
    assert "알 수 없는 오류" in friendly_error("   ")


def _orch(max_turns=10, token_limit=None, agents=None):
    names = ["철수", "영희"]
    config = SessionConfig(
        topic="t",
        agents=[AgentConfig(name=n) for n in names],
        max_turns=max_turns,
        token_limit=token_limit,
    )
    agents = agents or [FakeAgent(n) for n in names]
    return Orchestrator(config, agents), agents


async def test_agent_error_shows_actionable_notice():
    # 가짜 에이전트는 fail_on 차례에 RuntimeError("네트워크 오류")를 낸다
    orch, _ = _orch(agents=[FakeAgent("철수"), FakeAgent("영희", fail_on={1})])
    await orch.run()
    assert orch.state == ConversationState.PAUSED
    assert "네트워크 연결" in orch.transcript[-1].text


async def test_token_limit_pauses_then_extends_on_resume():
    # 가짜 에이전트는 턴마다 15토큰 (입력 10 + 출력 5)
    orch, _ = _orch(token_limit=40)
    await orch.run()
    assert orch.state == ConversationState.PAUSED
    assert orch.turn == 3 and orch.total_tokens == 45  # 30 < 40 이라 3번째 턴까지 진행
    assert "상한(40)" in orch.transcript[-1].text

    await orch.run()
    # 재개하면 상한이 지금 사용량 + 설정값(40) = 85로 늘어나 3턴 더 진행한다
    assert orch.token_limit == 85
    assert any("85로 늘려" in m.text for m in orch.transcript)
    assert orch.turn == 6 and orch.state == ConversationState.PAUSED


async def test_token_limit_persists_in_snapshot():
    orch, _ = _orch(token_limit=40)
    await orch.run()
    await orch.run()
    snap = orch.snapshot()
    assert snap.token_limit == 85

    restored, _ = _orch(token_limit=40)
    restored.restore(snap, list(orch.transcript))
    assert restored.token_limit == 85 and restored.total_tokens == orch.total_tokens


async def test_no_token_limit_by_default():
    orch, _ = _orch(max_turns=4)
    await orch.run()
    assert orch.turn == 4 and orch.token_limit is None
