"""M6: 진행자 지목, 종료 제안·투표, 진행자 선언."""

import asyncio

import pytest

from app.core.agent_runner import TurnResult
from app.core.end_vote import EndVote, EndVoteError
from app.core.events import ModeratorDecided
from app.core.models import AgentConfig, ConversationState, EndPolicy, SessionConfig, TurnMode
from app.core.moderator import ModeratorDecision, ModeratorError, parse_decision
from app.core.orchestrator import Orchestrator
from app.core.prompts import build_system_prompt
from app.core.tools import build_end_tools
from tests.fakes import FakeAgent, SlowAgent

NAMES = ["철수", "영희", "민수"]


# ── 종료 투표 ──────────────────────────────────────
def _vote(policy):
    v = EndVote(policy, NAMES)
    notices: list[str] = []
    v.subscribe(notices.append)
    return v, notices


def test_unanimous_requires_everyone():
    v, notices = _vote(EndPolicy.UNANIMOUS)
    v.propose("철수", "결론 났음")
    assert v.ended_reason is None
    assert v.pending_voters() == ["영희", "민수"]
    assert "vote_end" in v.reminder_for("영희")
    assert v.reminder_for("철수") is None  # 제안자는 찬성으로 친다
    v.vote("영희", True, "")
    assert v.ended_reason is None
    v.vote("민수", True, "동의")
    assert v.ended_reason is not None and "전원 동의" in v.ended_reason
    assert any("전원이" in n for n in notices)


def test_unanimous_one_no_rejects_and_allows_new_proposal():
    v, notices = _vote(EndPolicy.UNANIMOUS)
    v.propose("철수", "x")
    v.vote("영희", False, "아직 할 말 있음")
    assert v.proposal is None and v.ended_reason is None
    assert "부결" in notices[-1]
    with pytest.raises(EndVoteError):
        v.vote("민수", True, "")  # 부결된 제안에는 투표할 수 없다
    v.propose("민수", "이제는 끝")
    assert v.proposal.id == 2


@pytest.mark.parametrize(
    "action",
    [
        lambda v: v.vote("영희", True, ""),  # 제안 없음
        lambda v: (v.propose("철수", "x"), v.propose("영희", "y")),  # 중복 제안
        lambda v: (v.propose("철수", "x"), v.vote("철수", True, "")),  # 제안자 재투표
        lambda v: v.propose("없는사람", "x"),
    ],
)
def test_unanimous_invalid_actions(action):
    v, _ = _vote(EndPolicy.UNANIMOUS)
    with pytest.raises(EndVoteError):
        action(v)


def test_any_policy_ends_immediately():
    v, _ = _vote(EndPolicy.ANY)
    v.propose("민수", "충분히 이야기함")
    assert v.ended_reason is not None
    with pytest.raises(EndVoteError):
        v.vote("철수", True, "")


def test_moderator_policy_only_records_proposal():
    v, notices = _vote(EndPolicy.MODERATOR)
    v.propose("철수", "끝내자")
    assert v.ended_reason is None and v.pending_voters() == []
    assert "진행자가 판단" in notices[-1]
    with pytest.raises(EndVoteError):
        v.vote("영희", True, "")
    v.declare("게임 종료")
    assert v.ended_reason == "진행자 선언"


def test_end_tools_follow_policy():
    names = lambda p: [t.name for t in build_end_tools(EndVote(p, NAMES), "철수")]  # noqa: E731
    assert names(EndPolicy.UNANIMOUS) == ["propose_end", "vote_end"]
    assert names(EndPolicy.ANY) == ["propose_end"]
    assert names(EndPolicy.MODERATOR) == ["propose_end"]
    v = EndVote(EndPolicy.UNANIMOUS, NAMES)
    tools = {t.name: t.handler for t in build_end_tools(v, "영희")}
    assert tools["vote_end"]({"agree": True}).is_error  # 제안 없음
    assert not tools["propose_end"]({"reason": "끝"}).is_error
    assert tools["vote_end"]({"agree": "네"}).is_error  # bool 아님


def test_system_prompt_mentions_end_policy():
    for policy, word in [
        (EndPolicy.UNANIMOUS, "vote_end"),
        (EndPolicy.ANY, "바로 끝난다"),
        (EndPolicy.MODERATOR, "진행자가 정한다"),
    ]:
        mode = TurnMode.MODERATOR if policy == EndPolicy.MODERATOR else TurnMode.ROUND_ROBIN
        config = SessionConfig(
            topic="t",
            agents=[AgentConfig(name=n) for n in NAMES],
            end_policy=policy,
            turn_mode=mode,
        )
        assert word in build_system_prompt(config, config.agents[0])


def test_moderator_end_policy_requires_moderator_mode():
    with pytest.raises(ValueError, match="진행자 지목 모드"):
        SessionConfig(
            topic="t", agents=[AgentConfig(name=n) for n in NAMES], end_policy=EndPolicy.MODERATOR
        )


# ── 진행자 응답 해석 ───────────────────────────────
def test_parse_decision():
    assert parse_decision('{"next": "영희", "reason": "질문받음", "end": false}', NAMES) == (
        "영희",
        "질문받음",
        False,
    )
    wrapped = '좋습니다.\n```json\n{"next": "@민수", "reason": "r"}\n```'
    assert parse_decision(wrapped, NAMES) == ("민수", "r", False)
    assert parse_decision('{"next": null, "reason": "승부 남", "end": true}', NAMES) == (
        None,
        "승부 남",
        True,
    )
    for bad in ["영희가 좋겠어요", '{"next": "없는사람"}', '{"next": "영희", "end": "yes"}', "{"]:
        with pytest.raises(ModeratorError):
            parse_decision(bad, NAMES)


# ── 오케스트레이터 + 가짜 진행자 ────────────────────
class FakeModerator:
    def __init__(self, picks, fail_on=(), slow=False):
        self.picks = list(picks)  # ModeratorDecision 또는 예외
        self.prompts: list[str] = []
        self.fail_on = set(fail_on)
        self.slow = slow
        self.started = asyncio.Event()
        self.disconnected = False

    async def decide(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        if self.slow:
            await asyncio.sleep(10)
        if len(self.prompts) in self.fail_on:
            raise ModeratorError("엉뚱한 답", usage=TurnResult("x", "s", None, 3, 1, False))
        pick = self.picks.pop(0)
        return ModeratorDecision(pick[0], pick[1], pick[2] if len(pick) > 2 else False)

    async def disconnect(self):
        self.disconnected = True


def _mod_orch(agents, moderator, policy=EndPolicy.UNANIMOUS, max_turns=3):
    config = SessionConfig(
        topic="t",
        agents=[AgentConfig(name=a.name) for a in agents],
        turn_mode=TurnMode.MODERATOR,
        end_policy=policy,
        max_turns=max_turns,
    )
    events: list = []
    end_vote = EndVote(policy, config.agent_names)
    orch = Orchestrator(
        config, agents, events.append, moderator=moderator, end_vote=end_vote, interrupt_grace=0.5
    )
    return orch, events


def _speakers(orch):
    return [m.speaker for m in orch.transcript if m.kind == "agent"]


async def test_moderator_picks_speakers():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("민수", "a"), ("민수", "b"), ("철수", "c")])
    orch, events = _mod_orch(agents, mod)
    await orch.run()
    assert _speakers(orch) == ["민수", "민수", "철수"]
    decided = [e for e in events if isinstance(e, ModeratorDecided)]
    assert [(e.next, e.reason) for e in decided] == [("민수", "a"), ("민수", "b"), ("철수", "c")]
    # 진행자는 매번 지난 판단 이후 메시지 + 현황을 받는다
    assert "[참가자 현황]" in mod.prompts[1] and "민수: 발언 1회" in mod.prompts[1]
    assert "민수: 민수의 1번째 발언" in mod.prompts[1]
    assert "민수: 민수의 1번째 발언" not in mod.prompts[2]
    await orch.close()
    assert mod.disconnected


async def test_moderator_failure_falls_back_to_round_robin():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("민수", "r")], fail_on={1})
    orch, events = _mod_orch(agents, mod, max_turns=2)
    await orch.run()
    assert _speakers(orch) == ["철수", "민수"]  # 1번: 실패→순서대로(철수), 2번: 진행자가 민수
    fallback = [e for e in events if isinstance(e, ModeratorDecided) and e.fallback]
    assert len(fallback) == 1 and fallback[0].next == "철수"
    assert orch.usage["(진행자)"] == (3, 1)  # 실패한 응답의 사용량도 센다


async def test_mention_skips_moderator():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("철수", "r")])
    orch, _ = _mod_orch(agents, mod, max_turns=2)
    await orch.send_user_message("@영희 먼저")
    await orch.run()
    assert _speakers(orch) == ["영희", "철수"]
    assert len(mod.prompts) == 1


async def test_user_message_cancels_pending_moderator_decision():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("민수", "r")], slow=True)
    orch, _ = _mod_orch(agents, mod, max_turns=1)
    run = asyncio.create_task(orch.run())
    await mod.started.wait()
    mod.slow = False
    await orch.send_user_message("다들 짧게")
    await run
    assert _speakers(orch) == ["민수"]
    assert len(mod.prompts) == 2 and "[사용자]: 다들 짧게" in mod.prompts[1]


async def test_pause_during_moderator_decision():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([], slow=True)
    orch, _ = _mod_orch(agents, mod)
    run = asyncio.create_task(orch.run())
    await mod.started.wait()
    await orch.pause()
    await asyncio.wait_for(run, 2)
    assert orch.state == ConversationState.PAUSED and _speakers(orch) == []


async def test_moderator_declares_end():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("영희", "r"), (None, "승부가 났다", True)])
    orch, _ = _mod_orch(agents, mod, policy=EndPolicy.MODERATOR, max_turns=10)
    await orch.run()
    assert orch.state == ConversationState.ENDED
    assert _speakers(orch) == ["영희"]
    texts = [m.text for m in orch.transcript if m.kind == "system"]
    assert any("진행자가 대화 종료를 선언" in t for t in texts)
    assert "대화를 마칩니다 (진행자 선언)" in texts[-1]
    with pytest.raises(RuntimeError):
        await orch.run()


async def test_moderator_end_ignored_unless_policy_allows():
    agents = [FakeAgent(n) for n in NAMES]
    mod = FakeModerator([("영희", "r", True), ("철수", "r")])
    orch, _ = _mod_orch(agents, mod, policy=EndPolicy.UNANIMOUS, max_turns=2)
    await orch.run()
    assert orch.state == ConversationState.PAUSED and _speakers(orch) == ["영희", "철수"]


# ── 투표로 끝나는 대화 ─────────────────────────────
class VotingAgent(FakeAgent):
    """정해진 차례에 종료 도구를 부르는 에이전트 (실제 에이전트의 도구 호출 대신)."""

    def __init__(self, name, end_vote, actions):
        super().__init__(name)
        self.end_vote = end_vote
        self.actions = actions  # {몇 번째 발언: ("propose"|"yes"|"no")}

    async def run_turn(self, prompt, on_text=None, on_tool=None):
        act = self.actions.get(len(self.prompts) + 1)
        if act == "propose":
            self.end_vote.propose(self.name, "할 말 다 했음")
        elif act in ("yes", "no"):
            self.end_vote.vote(self.name, act == "yes", "")
        return await super().run_turn(prompt, on_text, on_tool)


async def test_unanimous_vote_ends_round_robin_conversation():
    config = SessionConfig(topic="t", agents=[AgentConfig(name=n) for n in NAMES], max_turns=20)
    v = EndVote(EndPolicy.UNANIMOUS, NAMES)
    a = VotingAgent("철수", v, {1: "propose"})
    b = VotingAgent("영희", v, {1: "yes"})
    c = VotingAgent("민수", v, {1: "no", 2: "propose"})
    orch = Orchestrator(config, [a, b, c], end_vote=v)
    await orch.run()
    # 1회차: 철수 제안 → 영희 찬성 → 민수 반대(부결) / 2회차: 철수, 영희 무표 → 민수 제안
    # 이후 철수·영희가 찬성해야 하지만 actions가 없으므로 계속 상기만 받는다 → 최대 턴까지
    assert orch.state == ConversationState.PAUSED
    assert "[종료 투표]" in a.prompts[2]
    assert "[종료 투표]" not in c.prompts[2]  # 제안자에게는 상기하지 않는다

    v2 = EndVote(EndPolicy.UNANIMOUS, NAMES)
    a2 = VotingAgent("철수", v2, {2: "yes"})
    b2 = VotingAgent("영희", v2, {1: "propose"})
    c2 = VotingAgent("민수", v2, {1: "yes"})
    orch2 = Orchestrator(config, [a2, b2, c2], end_vote=v2)
    await orch2.run()
    assert orch2.state == ConversationState.ENDED
    assert [m.speaker for m in orch2.transcript if m.kind == "agent"] == [
        "철수",
        "영희",
        "민수",
        "철수",
    ]
    assert "전원 동의" in orch2.transcript[-1].text


async def test_end_decided_during_interrupted_turn_still_ends():
    config = SessionConfig(topic="t", agents=[AgentConfig(name=n) for n in NAMES[:2]])
    v = EndVote(EndPolicy.ANY, NAMES[:2])
    slow = SlowAgent("철수", chunks=500)
    orch = Orchestrator(config, [slow, FakeAgent("영희")], end_vote=v, interrupt_grace=0.5)
    run = asyncio.create_task(orch.run())
    await slow.started.wait()
    v.propose("철수", "끝")  # 도구 호출이 끝난 뒤 사용자가 끼어든 상황
    await orch.send_user_message("잠깐")
    await run
    assert orch.state == ConversationState.ENDED
