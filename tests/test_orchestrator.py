from collections.abc import Callable

import pytest

from app.core.agent_runner import TurnResult
from app.core.events import StateChanged, TextDelta, TurnCompleted
from app.core.models import AgentConfig, ConversationState, SessionConfig
from app.core.orchestrator import Orchestrator, strip_own_name_prefix


class FakeAgent:
    def __init__(self, name: str, fail_on: set[int] | None = None) -> None:
        self.name = name
        self.prompts: list[str] = []
        self.fail_on = fail_on or set()
        self.disconnected = False

    async def run_turn(self, prompt: str, on_text: Callable[[str], None] | None = None):
        self.prompts.append(prompt)
        n = len(self.prompts)
        if n in self.fail_on:
            raise RuntimeError("네트워크 오류")
        text = f"{self.name}의 {n}번째 발언"
        if on_text:
            on_text(text)
        return TurnResult(
            text=text,
            session_id="s",
            cost_usd=None,
            input_tokens=10,
            output_tokens=5,
            is_error=False,
        )

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


def _setup(names: list[str], max_turns: int, **fail):
    config = SessionConfig(
        topic="테스트 주제", agents=[AgentConfig(name=n) for n in names], max_turns=max_turns
    )
    agents = [FakeAgent(n, fail.get(n)) for n in names]
    events: list = []
    return Orchestrator(config, agents, events.append), agents, events


async def test_round_robin_order_and_stops_at_max_turns():
    orch, agents, events = _setup(["철수", "영희", "민수"], max_turns=5)
    await orch.run()
    speakers = [m.speaker for m in orch.transcript if m.kind == "agent"]
    assert speakers == ["철수", "영희", "민수", "철수", "영희"]
    assert orch.turn == 5
    assert orch.state == ConversationState.PAUSED
    assert orch.transcript[-1].kind == "system"
    assert [e.state for e in events if isinstance(e, StateChanged)] == [
        ConversationState.RUNNING,
        ConversationState.PAUSED,
    ]


async def test_update_contains_others_but_not_own_messages():
    orch, (a, b), _ = _setup(["철수", "영희"], max_turns=4)
    await orch.run()
    assert "[대화 시작]" in a.prompts[0]
    assert "철수: 철수의 1번째 발언" in b.prompts[0]
    second = a.prompts[1]
    assert "영희: 영희의 1번째 발언" in second
    assert "철수의 1번째 발언" not in second
    assert "이제 당신(철수)의 차례입니다." in second


async def test_add_turns_resumes_from_next_speaker():
    orch, _, _ = _setup(["철수", "영희", "민수"], max_turns=2)
    await orch.run()
    orch.add_turns(2)
    await orch.run()
    speakers = [m.speaker for m in orch.transcript if m.kind == "agent"]
    assert speakers == ["철수", "영희", "민수", "철수"]


async def test_error_pauses_without_counting_turn():
    orch, (a, b), _ = _setup(["철수", "영희"], max_turns=10, 영희={1})
    await orch.run()
    assert orch.state == ConversationState.PAUSED
    assert orch.turn == 1
    assert "오류" in orch.transcript[-1].text
    # 재개하면 실패했던 영희부터 다시, 철수의 발언도 다시 전달된다
    orch.max_turns = 2
    await orch.run()
    assert [m.speaker for m in orch.transcript if m.kind == "agent"] == ["철수", "영희"]
    assert "철수: 철수의 1번째 발언" in b.prompts[1]


async def test_streaming_and_usage_events():
    orch, _, events = _setup(["철수", "영희"], max_turns=2)
    await orch.run()
    assert [e.agent for e in events if isinstance(e, TextDelta)] == ["철수", "영희"]
    assert len([e for e in events if isinstance(e, TurnCompleted)]) == 2
    assert orch.usage["철수"] == (10, 5)


async def test_close_disconnects_all():
    orch, agents, _ = _setup(["철수", "영희"], max_turns=1)
    await orch.close()
    assert all(a.disconnected for a in agents)


def test_agent_list_must_match_config():
    config = SessionConfig(topic="t", agents=[AgentConfig(name="철수"), AgentConfig(name="영희")])
    with pytest.raises(ValueError):
        Orchestrator(config, [FakeAgent("영희"), FakeAgent("철수")])


def test_strip_own_name_prefix():
    assert strip_own_name_prefix("영희", "영희: 비빔밥 추천") == "비빔밥 추천"
    assert strip_own_name_prefix("영희", "**영희:** 비빔밥") == "비빔밥"
    assert strip_own_name_prefix("영희", "철수: 남의 이름은 유지") == "철수: 남의 이름은 유지"
    assert strip_own_name_prefix("영희", "  본문만  ") == "본문만"


def test_config_validation():
    with pytest.raises(ValueError):
        SessionConfig(topic="t", agents=[AgentConfig(name="철수")])
    with pytest.raises(ValueError):
        SessionConfig(topic="t", agents=[AgentConfig(name="철수"), AgentConfig(name="철수")])
