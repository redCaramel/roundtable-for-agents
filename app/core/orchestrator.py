"""대화 진행 루프와 상태 머신 (plan.md 2장)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
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
    TurnInterrupted,
    TurnStarted,
    UsageUpdated,
    UserMessageAdded,
)
from app.core.models import ConversationState, Message, SessionConfig
from app.core.prompts import build_turn_update

log = logging.getLogger(__name__)

# interrupt() 후 턴이 스스로 끝나기를 기다리는 시간. 넘기면 태스크를 취소한다.
INTERRUPT_GRACE_SECONDS = 10.0


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


def find_mention(text: str, names: Iterable[str]) -> str | None:
    """메시지에서 처음 나오는 `@이름`을 찾는다.

    긴 이름부터 맞춰 '@철수2'를 '철수'로 오인하지 않는다.
    """
    ordered = sorted(names, key=len, reverse=True)
    for i, ch in enumerate(text):
        if ch != "@":
            continue
        for name in ordered:
            if text.startswith(name, i + 1):
                return name
    return None


class Orchestrator:
    def __init__(
        self,
        config: SessionConfig,
        agents: Sequence[Agent],
        emit: EventSink = _ignore,
        acl: AccessControl | None = None,
        interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
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
        self.interrupt_grace = interrupt_grace
        # 진행 중인 턴과 중단 요청 사유
        self._turn_task: asyncio.Future[TurnResult] | None = None
        self._turn_agent: Agent | None = None
        self._interrupt_reason: str | None = None
        # 직전 발언이 중단되어 다음 차례에 알려줘야 하는 에이전트
        self._interrupted: set[str] = set()
        # @지목으로 정해진 다음 발언자
        self._forced_next: str | None = None
        # 사용자 개입을 처리하는 동안 다음 턴이 시작되지 않게 막는다
        self._gate = asyncio.Event()
        self._gate.set()
        self._loop_active = False
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

    # ── 사용자 제어 (plan.md 7장) ────────────────────
    @property
    def speaking(self) -> str | None:
        """지금 발언 중인 에이전트 이름."""
        return self._turn_agent.name if self._turn_agent is not None else None

    async def send_user_message(self, text: str) -> Message:
        """사용자 개입. 진행 중이면 현재 발언을 즉시 끊고 반영한 뒤 이어간다.

        `@이름`이 있으면 그 에이전트가 다음 발언자가 된다. 일시정지 중이면 메시지만 쌓인다.
        """
        if self.state == ConversationState.ENDED:
            raise RuntimeError("종료된 대화에는 메시지를 보낼 수 없습니다.")
        text = text.strip()
        if not text:
            raise ValueError("빈 메시지입니다.")
        self._gate.clear()
        try:
            await self._interrupt_current("user")
            msg = Message(kind="user", text=text)
            self.transcript.append(msg)
            self._forced_next = find_mention(text, self.config.agent_names) or self._forced_next
            self._emit(UserMessageAdded(msg))
            return msg
        finally:
            self._gate.set()

    async def pause(self) -> None:
        """즉시 중단하고 PAUSED. 재개하면 중단됐던 에이전트부터 다시 한다."""
        if self.state != ConversationState.RUNNING:
            return
        self._set_state(ConversationState.PAUSED)
        await self._interrupt_current("pause")

    async def stop(self) -> None:
        """즉시 중단하고 ENDED."""
        if self.state == ConversationState.ENDED:
            return
        self._set_state(ConversationState.ENDED)
        await self._interrupt_current("stop")
        self._notice("사용자가 대화를 종료했습니다.")

    async def _interrupt_current(self, reason: str) -> None:
        task, agent = self._turn_task, self._turn_agent
        if task is None or task.done() or agent is None:
            return
        self._interrupt_reason = reason
        try:
            await asyncio.wait_for(agent.interrupt(), timeout=self.interrupt_grace)
        except Exception:
            log.warning("[%s] interrupt 실패 — 태스크 취소로 넘어감", agent.name, exc_info=True)
        done, _ = await asyncio.wait({task}, timeout=self.interrupt_grace)
        if not done:
            log.warning("[%s] interrupt 후에도 턴이 끝나지 않아 취소합니다", agent.name)
            task.cancel()
            await asyncio.wait({task})

    # ── 진행 ─────────────────────────────────────────
    def next_speaker(self) -> Agent:
        """@지목이 있으면 그 에이전트, 아니면 라운드로빈. 진행자 모드는 M6에서 추가한다."""
        if self._forced_next is not None:
            self._next_index = self.config.agent_names.index(self._forced_next)
            self._forced_next = None
        return self.agents[self._next_index]

    async def run(self) -> None:
        """PAUSED/ENDED가 될 때까지 턴을 돌린다."""
        if self.state == ConversationState.ENDED:
            raise RuntimeError("종료된 대화입니다.")
        if self._loop_active:
            raise RuntimeError("이미 진행 중입니다.")
        self._loop_active = True
        try:
            self._set_state(ConversationState.RUNNING)
            while self.state == ConversationState.RUNNING:
                await self._gate.wait()
                if self.state != ConversationState.RUNNING:
                    break
                if self.turn >= self.max_turns:
                    self._notice(f"최대 턴({self.max_turns})에 도달해 일시정지합니다.")
                    self._set_state(ConversationState.PAUSED)
                    break
                if self.acl is not None:
                    self.acl.expire(self.turn)
                await self._take_turn(self.next_speaker())
        finally:
            self._loop_active = False

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
            agent.name,
            new,
            is_first_turn=agent.name not in self._spoken,
            reminders=reminders,
            was_interrupted=agent.name in self._interrupted,
        )
        snapshot = len(self.transcript)

        turn_no = self.turn + 1
        self._emit(TurnStarted(agent.name, turn_no))
        self._turn_agent = agent
        self._turn_task = asyncio.ensure_future(
            agent.run_turn(
                prompt,
                on_text=lambda t: self._emit(TextDelta(agent.name, t)),
                on_tool=lambda tool, inp, d: self._emit(
                    ToolActivity(agent.name, tool, inp, denied=not d.allowed, reason=d.reason)
                ),
            )
        )
        result: TurnResult | None = None
        error: Exception | None = None
        try:
            result = await self._turn_task
        except asyncio.CancelledError:
            # 우리가 중단시킨 취소만 삼킨다. 바깥(run 태스크) 취소는 그대로 올려보낸다.
            current = asyncio.current_task()
            if self._interrupt_reason is None or (current is not None and current.cancelling()):
                raise
        except Exception as e:
            error = e
        finally:
            self._turn_task = None
            self._turn_agent = None

        reason, self._interrupt_reason = self._interrupt_reason, None
        if result is not None:
            self._record_usage(agent.name, result)
        if reason is not None:
            # 미완성 발언은 폐기한다. 업데이트 프롬프트는 세션에 이미 들어갔으므로 커서는 넘긴다.
            self._cursor[agent.name] = snapshot
            self._spoken.add(agent.name)
            self._interrupted.add(agent.name)
            self._emit(TurnInterrupted(agent.name, reason))
            return
        self._interrupted.discard(agent.name)

        if error is not None:  # 로그인 만료·네트워크 오류 등 — 자동 재시도하지 않는다
            log.error("에이전트 %s 턴 실패", agent.name, exc_info=error)
            self._notice(f"{agent.name}의 발언 중 오류가 발생해 일시정지합니다: {error}")
            self._set_state(ConversationState.PAUSED)
            return

        assert result is not None
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
        await self._interrupt_current("stop")
        for agent in self.agents:
            try:
                await agent.disconnect()
            except Exception:
                log.exception("에이전트 %s 연결 종료 실패", agent.name)
