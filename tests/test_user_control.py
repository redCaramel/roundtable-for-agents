"""사용자 제어 (M5): 개입 즉시 중단, 일시정지/재개, 종료, @지목."""

import asyncio

import pytest

from app.core.events import TurnInterrupted, UserMessageAdded
from app.core.models import AgentConfig, ConversationState, SessionConfig
from app.core.orchestrator import Orchestrator, find_mention
from tests.fakes import FakeAgent, SlowAgent


def _orch(agents, max_turns=10, grace=1.0):
    config = SessionConfig(
        topic="t", agents=[AgentConfig(name=a.name) for a in agents], max_turns=max_turns
    )
    events: list = []
    return Orchestrator(config, agents, events.append, interrupt_grace=grace), events


def _speakers(orch):
    return [m.speaker for m in orch.transcript if m.kind == "agent"]


def test_find_mention():
    names = ["철수", "철수2", "영희"]
    assert find_mention("@영희 의견은?", names) == "영희"
    assert find_mention("둘 다 @철수2 먼저", names) == "철수2"
    assert find_mention("@철수 그리고 @영희", names) == "철수"
    assert find_mention("메일 a@b.com", names) is None
    assert find_mention("@없는사람", names) is None


async def test_user_message_interrupts_and_discards_partial_reply():
    a, b = SlowAgent("철수"), SlowAgent("영희", chunks=3)
    orch, events = _orch([a, b], max_turns=2)
    run = asyncio.create_task(orch.run())
    await a.started.wait()
    await orch.send_user_message("잠깐, 짧게 말해 줘")
    await run

    interrupted = [e for e in events if isinstance(e, TurnInterrupted)]
    assert [(e.agent, e.reason) for e in interrupted] == [("철수", "user")]
    # 미완성 발언은 기록되지 않고, 턴도 세지 않는다. 철수가 다시 말한다.
    assert _speakers(orch) == ["철수", "영희"]
    assert orch.transcript[0].kind == "user"
    assert "[알림] 당신의 직전 발언은 중단되어" in a.prompts[1]
    assert "[사용자]: 잠깐, 짧게 말해 줘" in a.prompts[1]
    assert "[사용자]: 잠깐, 짧게 말해 줘" in b.prompts[0]
    assert "[알림]" not in b.prompts[0]
    # 화면 순서: 중단 → 사용자 말풍선
    kinds = [type(e) for e in events if isinstance(e, TurnInterrupted | UserMessageAdded)]
    assert kinds == [TurnInterrupted, UserMessageAdded]


async def test_mention_picks_next_speaker_then_round_robin_continues():
    agents = [SlowAgent(n, chunks=2) for n in ("철수", "영희", "민수")]
    orch, _ = _orch(agents, max_turns=3)
    run = asyncio.create_task(orch.run())
    await agents[0].started.wait()
    await orch.send_user_message("@민수 먼저 말해 줘")
    await run
    assert _speakers(orch) == ["민수", "철수", "영희"]


async def test_user_message_while_paused_is_queued():
    a, b = FakeAgent("철수"), FakeAgent("영희")
    orch, _ = _orch([a, b], max_turns=1)
    await orch.run()
    assert orch.state == ConversationState.PAUSED
    await orch.send_user_message("@철수 한 번 더")
    assert orch.state == ConversationState.PAUSED  # 자동으로 재개하지 않는다
    orch.add_turns(1)
    await orch.run()
    assert _speakers(orch) == ["철수", "철수"]
    assert "[사용자]: @철수 한 번 더" in a.prompts[1]


async def test_pause_then_resume_restarts_interrupted_agent():
    a, b = SlowAgent("철수"), SlowAgent("영희", chunks=2)
    orch, events = _orch([a, b], max_turns=2)
    run = asyncio.create_task(orch.run())
    await a.started.wait()
    await orch.pause()
    await run
    assert orch.state == ConversationState.PAUSED
    assert orch.turn == 0 and _speakers(orch) == []
    assert [e.reason for e in events if isinstance(e, TurnInterrupted)] == ["pause"]

    a.chunks = 2
    await orch.run()
    assert _speakers(orch) == ["철수", "영희"]
    assert "[알림] 당신의 직전 발언은 중단되어" in a.prompts[1]


async def test_stop_ends_conversation():
    a, b = SlowAgent("철수"), SlowAgent("영희")
    orch, events = _orch([a, b])
    run = asyncio.create_task(orch.run())
    await a.started.wait()
    await orch.stop()
    await run
    assert orch.state == ConversationState.ENDED
    assert _speakers(orch) == []
    assert "종료" in orch.transcript[-1].text
    with pytest.raises(RuntimeError):
        await orch.send_user_message("이어서")
    with pytest.raises(RuntimeError):
        await orch.run()


async def test_stuck_agent_is_cancelled_after_grace():
    a, b = SlowAgent("철수", chunks=1000, stuck=True), SlowAgent("영희", chunks=1)
    orch, events = _orch([a, b], max_turns=1, grace=0.05)
    run = asyncio.create_task(orch.run())
    await a.started.wait()
    await orch.send_user_message("@영희 대신 말해 줘")
    await run
    assert a.cancelled  # interrupt가 안 먹혀서 태스크를 취소했다
    assert _speakers(orch) == ["영희"]
    assert [e.agent for e in events if isinstance(e, TurnInterrupted)] == ["철수"]


async def test_turn_finishing_before_pause_is_kept():
    a, b = FakeAgent("철수"), FakeAgent("영희")
    orch, _ = _orch([a, b], max_turns=5)
    await orch.pause()  # 진행 중이 아니면 아무 일도 없다
    assert orch.state == ConversationState.IDLE
    await orch.run()
    assert len(_speakers(orch)) == 5


async def test_outer_cancel_propagates():
    a, b = SlowAgent("철수", chunks=1000), SlowAgent("영희")
    orch, _ = _orch([a, b])
    run = asyncio.create_task(orch.run())
    await a.started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert a.cancelled


async def test_empty_message_rejected():
    orch, _ = _orch([FakeAgent("철수"), FakeAgent("영희")])
    with pytest.raises(ValueError):
        await orch.send_user_message("   ")
