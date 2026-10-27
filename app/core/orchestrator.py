"""대화 진행 루프와 상태 머신 (plan.md 2장)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from app.core.access_control import AccessControl
from app.core.agent_runner import ToolCallback, TurnResult
from app.core.events import (
    Event,
    EventSink,
    StateChanged,
    SystemNotice,
    TextDelta,
    ToolActivity,
    TurnCompleted,
    TurnStarted,
    UsageUpdated,
)
from app.core.models import ConversationState, Message, SessionConfig
from app.core.prompts import build_turn_update

log = logging.getLogger(__name__)


class Agent(Protocol):
    """오케스트레이터가 기대하는 에이전트. 실제 구현은 AgentRunner, 테스트는 가짜 에이전트."""

    name: str

    async def run_turn(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_tool: ToolCallback | None = None,
    ) -> TurnResult: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


def _ignore(_: Event) -> None:
    pass


def strip_own_name_prefix(name: str, text: str) -> str:
    """업데이트 형식("철수: ...")을 흉내 내 발언 앞에 붙인 자기 이름 접두를 떼어낸다."""
    text = text.strip()
    for prefix in (f"{name}:", f"**{name}:**", f"**{name}**:"):
        if text.startswith(prefix):
            return text[len(prefix) :].lstrip()
    return text


class Orchestrator:
    def __init__(
        self,
        config: SessionConfig,
        agents: Sequence[Agent],
        emit: EventSink = _ignore,
        acl: AccessControl | None = None,
    ) -> None:
        if [a.name for a in agents] != config.agent_names:
            raise ValueError("에이전트 목록이 설정의 순서·이름과 일치해야 합니다.")
        self.config = config
        self.agents = list(agents)
        self._emit = emit
        self.state = ConversationState.IDLE
        self.transcript: list[Message] = []
        self.turn = 0
        self.max_turns = config.max_turns
        self._next_index = 0
        # 각 에이전트에게 transcript의 어디까지 전달했는지
        self._cursor: dict[str, int] = {a.name: 0 for a in self.agents}
        self._spoken: set[str] = set()
        self.usage: dict[str, tuple[int, int]] = {a.name: (0, 0) for a in self.agents}
        self.acl = acl
        if acl is not None:
            # 열람 요청·허가·철회는 채팅에 시스템 메시지로 공개한다.
            acl.subscribe(self._notice)

    # ── 상태 ─────────────────────────────────────────
    def _set_state(self, state: ConversationState) -> None:
        if state != self.state:
            self.state = state
            self._emit(StateChanged(state))

    def _notice(self, text: str) -> Message:
        msg = Message(kind="system", text=text)
        self.transcript.append(msg)
        self._emit(SystemNotice(msg))
        return msg

    def add_turns(self, n: int) -> None:
        """최대 턴에 도달해 멈춘 대화에 턴을 더한다."""
        if n < 1:
            raise ValueError("추가 턴은 1 이상이어야 합니다.")
        self.max_turns += n

    # ── 진행 ─────────────────────────────────────────
    def next_speaker(self) -> Agent:
        """라운드로빈. 진행자 모드·@지목은 M5~M6에서 추가한다."""
        return self.agents[self._next_index]

    async def run(self) -> None:
        """PAUSED/ENDED가 될 때까지 턴을 돌린다."""
        if self.state == ConversationState.ENDED:
            raise RuntimeError("종료된 대화입니다.")
        self._set_state(ConversationState.RUNNING)
        while self.state == ConversationState.RUNNING:
            if self.turn >= self.max_turns:
                self._notice(f"최대 턴({self.max_turns})에 도달해 일시정지합니다.")
                self._set_state(ConversationState.PAUSED)
                break
            if self.acl is not None:
                self.acl.expire(self.turn)
            await self._take_turn(self.next_speaker())

    async def _take_turn(self, agent: Agent) -> None:
        start = self._cursor[agent.name]
        # 자기 발언은 SDK 세션에 이미 기억되어 있으므로 빼고 보낸다.
        new = [
            m
            for m in self.transcript[start:]
            if not (m.kind == "agent" and m.speaker == agent.name)
        ]
        reminders = []
        if self.acl is not None:
            reminders = [
                f"#{r.id} {r.requester}가 당신 폴더 열람을 요청 중 (사유: {r.reason})"
                for r in self.acl.pending_for(agent.name)
            ]
        prompt = build_turn_update(
            agent.name, new, is_first_turn=agent.name not in self._spoken, reminders=reminders
        )
        snapshot = len(self.transcript)

        turn_no = self.turn + 1
        self._emit(TurnStarted(agent.name, turn_no))
        try:
            result = await agent.run_turn(
                prompt,
                on_text=lambda t: self._emit(TextDelta(agent.name, t)),
                on_tool=lambda tool, inp, d: self._emit(
                    ToolActivity(agent.name, tool, inp, denied=not d.allowed, reason=d.reason)
                ),
            )
        except Exception as e:  # 로그인 만료·네트워크 오류 등 — 자동 재시도하지 않는다
            log.exception("에이전트 %s 턴 실패", agent.name)
            self._notice(f"{agent.name}의 발언 중 오류가 발생해 일시정지합니다: {e}")
            self._set_state(ConversationState.PAUSED)
            return

        self._record_usage(agent.name, result)
        if result.is_error or not result.text.strip():
            reason = result.text.strip() or "빈 응답"
            self._notice(f"{agent.name}의 응답 오류로 일시정지합니다: {reason}")
            self._set_state(ConversationState.PAUSED)
            return

        self._cursor[agent.name] = snapshot
        self._spoken.add(agent.name)
        text = strip_own_name_prefix(agent.name, result.text)
        self._warn_if_impersonating(agent.name, text)
        msg = Message(kind="agent", speaker=agent.name, text=text, turn=turn_no)
        self.transcript.append(msg)
        self.turn = turn_no
        self._next_index = (self._next_index + 1) % len(self.agents)
        self._emit(TurnCompleted(msg))

    def _record_usage(self, name: str, result: TurnResult) -> None:
        i, o = self.usage[name]
        self.usage[name] = (i + (result.input_tokens or 0), o + (result.output_tokens or 0))
        self._emit(UsageUpdated(name, *self.usage[name]))

    def _warn_if_impersonating(self, name: str, text: str) -> None:
        for other in self.config.agent_names:
            if other == name:
                continue
            if any(line.lstrip().startswith(f"{other}:") for line in text.splitlines()):
                log.warning("%s의 발언에 다른 참가자(%s)의 대사가 섞였을 수 있습니다.", name, other)

    async def close(self) -> None:
        for agent in self.agents:
            try:
                await agent.disconnect()
            except Exception:
                log.exception("에이전트 %s 연결 종료 실패", agent.name)
