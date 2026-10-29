"""대화 진행 루프와 상태 머신 (plan.md 2장)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Protocol

from app.core.access_control import AccessControl
from app.core.agent_runner import ToolCallback, TurnResult
from app.core.end_vote import EndVote
from app.core.events import (
    Event,
    EventSink,
    ModeratorDecided,
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
from app.core.models import ConversationState, EndPolicy, Message, SessionConfig, TurnMode
from app.core.moderator import MODERATOR_USAGE_KEY, ModeratorDecision, ModeratorError
from app.core.prompts import build_moderator_update, build_turn_update
from app.core.storage import SessionSnapshot

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


class Store(Protocol):
    """자동 저장 대상. 실제 구현은 storage.SessionStore."""

    def append_message(self, msg: Message) -> None: ...

    def save_snapshot(self, snapshot: SessionSnapshot) -> None: ...

    async def aflush(self) -> None: ...

    def close(self) -> None: ...


class ModeratorLike(Protocol):
    """진행자. 실제 구현은 moderator.Moderator, 테스트는 가짜 진행자."""

    async def decide(self, prompt: str) -> ModeratorDecision: ...

    async def disconnect(self) -> None: ...


def _ignore(_: Event) -> None:
    pass


def strip_own_name_prefix(name: str, text: str) -> str:
    """업데이트 형식("철수: ...")을 흉내 내 줄 앞에 붙인 자기 이름 접두를 떼어낸다.

    도구 호출 앞뒤로 나뉜 발언은 조각마다 접두가 붙을 수 있어 줄 단위로 본다.
    """
    prefixes = (f"{name}:", f"**{name}:**", f"**{name}**:")
    lines = []
    for line in text.strip().splitlines():
        stripped = line.lstrip()
        for prefix in prefixes:
            if stripped.startswith(prefix):
                line = stripped[len(prefix) :].lstrip()
                break
        lines.append(line)
    return "\n".join(lines).strip()


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
        moderator: ModeratorLike | None = None,
        end_vote: EndVote | None = None,
        store: Store | None = None,
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
        self.end_vote = end_vote
        if end_vote is not None:
            end_vote.subscribe(self._notice)
        if config.turn_mode == TurnMode.MODERATOR and moderator is None:
            raise ValueError("진행자 지목 모드에는 진행자가 필요합니다.")
        self.moderator = moderator if config.turn_mode == TurnMode.MODERATOR else None
        # 진행자에게 transcript의 어디까지 전달했는지, 진행 중인 판단
        self._mod_cursor = 0
        self._mod_task: asyncio.Future[ModeratorDecision] | None = None
        self.store = store
        self._created_at: datetime | None = None

    # ── 상태 ─────────────────────────────────────────
    def _set_state(self, state: ConversationState) -> None:
        if state != self.state:
            self.state = state
            self._save()
            self._emit(StateChanged(state))

    def _notice(self, text: str) -> Message:
        msg = Message(kind="system", text=text)
        self._append(msg)
        self._emit(SystemNotice(msg))
        return msg

    def add_turns(self, n: int) -> None:
        """최대 턴에 도달해 멈춘 대화에 턴을 더한다."""
        if n < 1:
            raise ValueError("추가 턴은 1 이상이어야 합니다.")
        self.max_turns += n
        self._save()

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
            self._forced_next = find_mention(text, self.config.agent_names) or self._forced_next
            self._append(msg)
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
        mod = self._mod_task
        if mod is not None and not mod.done():
            # 진행자 판단은 버려도 되므로 바로 취소한다 (세션은 다음 판단 때 resume으로 다시 연다).
            mod.cancel()
            await asyncio.wait({mod})
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
        """@지목이 있으면 그 에이전트, 아니면 _next_index (라운드로빈 또는 진행자가 정한 자리)."""
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
                if self._end_if_decided():
                    break
                if self.acl is not None:
                    self.acl.expire(self.turn)
                speaker = await self._choose_speaker()
                if speaker is None:  # 진행자 판단이 취소되었거나 종료가 결정됨
                    continue
                await self._take_turn(speaker)
            self._end_if_decided()
        finally:
            self._loop_active = False

    def _end_if_decided(self) -> bool:
        """종료 제안·투표·진행자 선언으로 종료가 확정됐으면 ENDED로 바꾼다."""
        if self.end_vote is None or self.end_vote.ended_reason is None:
            return False
        if self.state != ConversationState.ENDED:
            self._set_state(ConversationState.ENDED)
            self._notice(f"대화를 마칩니다 ({self.end_vote.ended_reason}).")
        return True

    # ── 진행자 (plan.md 4.5) ─────────────────────────
    async def _choose_speaker(self) -> Agent | None:
        if self.moderator is None or self._forced_next is not None:
            return self.next_speaker()
        decision, cancelled = await self._ask_moderator()
        if cancelled or self.state != ConversationState.RUNNING or not self._gate.is_set():
            return None
        if self._forced_next is None and decision is not None:
            if decision.end and self.config.end_policy == EndPolicy.MODERATOR:
                if self.end_vote is not None:
                    self.end_vote.declare(decision.reason)
                    return None
            if decision.next is not None:
                self._next_index = self.config.agent_names.index(decision.next)
        return self.next_speaker()

    async def _ask_moderator(self) -> tuple[ModeratorDecision | None, bool]:
        """(판단, 취소됨). 판단에 실패하면 (None, False) — 호출자는 라운드로빈으로 대체한다."""
        assert self.moderator is not None
        snapshot = len(self.transcript)
        prompt = build_moderator_update(
            self.transcript[self._mod_cursor :], self._participant_status(), self._moderator_notes()
        )
        self._mod_task = asyncio.ensure_future(self.moderator.decide(prompt))
        try:
            decision = await self._mod_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return None, True
        except Exception as e:  # 해석 실패·연결 오류 — 대화는 멈추지 않고 순서대로 이어간다
            log.warning("진행자 판단 실패, 라운드로빈으로 대체: %s", e)
            if isinstance(e, ModeratorError) and e.usage is not None:
                self._record_usage(MODERATOR_USAGE_KEY, e.usage)
            self._mod_cursor = snapshot
            fallback = self.config.agent_names[self._next_index]
            self._emit(ModeratorDecided(fallback, f"진행자 판단 실패 — 순서대로 진행 ({e})", True))
            return None, False
        finally:
            self._mod_task = None
        self._mod_cursor = snapshot
        self._save()
        if decision.usage is not None:
            self._record_usage(MODERATOR_USAGE_KEY, decision.usage)
        self._emit(ModeratorDecided(decision.next, decision.reason, False, decision.end))
        return decision, False

    def _participant_status(self) -> list[str]:
        counts = {n: 0 for n in self.config.agent_names}
        last: dict[str, int] = {}
        for m in self.transcript:
            if m.kind == "agent" and m.speaker in counts:
                counts[m.speaker] += 1
                last[m.speaker] = m.turn or 0
        lines = []
        for n in self.config.agent_names:
            since = (
                "아직 발언 안 함" if n not in last else f"마지막 발언 후 {self.turn - last[n]}턴"
            )
            lines.append(f"{n}: 발언 {counts[n]}회, {since}")
        return lines

    def _moderator_notes(self) -> list[str]:
        notes = [f"남은 턴: {self.max_turns - self.turn}"]
        if self.acl is not None:
            for r in self.acl.requests:
                if r.status.value == "pending":
                    notes.append(
                        f"{r.target}가 응답해야 할 폴더 열람 요청 #{r.id}이 있음 "
                        f"(요청자 {r.requester})"
                    )
        if self.end_vote is not None and self.end_vote.proposal is not None:
            p = self.end_vote.proposal
            note = f"{p.proposer}가 대화 종료를 제안함 (사유: {p.reason})"
            waiting = self.end_vote.pending_voters()
            if waiting:
                note += f" — 아직 투표하지 않은 참가자: {', '.join(waiting)}"
            notes.append(note)
        return notes

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
            end_vote_reminder=self.end_vote.reminder_for(agent.name) if self.end_vote else None,
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
            self._save()
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
        self.turn = turn_no
        self._next_index = (self._next_index + 1) % len(self.agents)
        self._append(msg)
        self._emit(TurnCompleted(msg))

    def _record_usage(self, name: str, result: TurnResult) -> None:
        i, o = self.usage.get(name, (0, 0))
        self.usage[name] = (i + (result.input_tokens or 0), o + (result.output_tokens or 0))
        self._emit(UsageUpdated(name, *self.usage[name]))

    def _warn_if_impersonating(self, name: str, text: str) -> None:
        for other in self.config.agent_names:
            if other == name:
                continue
            if any(line.lstrip().startswith(f"{other}:") for line in text.splitlines()):
                log.warning("%s의 발언에 다른 참가자(%s)의 대사가 섞였을 수 있습니다.", name, other)

    # ── 저장 / 이어하기 (plan.md 8장) ────────────────
    def _append(self, msg: Message) -> None:
        """transcript에 확정 메시지를 더하고 바로 저장한다."""
        self.transcript.append(msg)
        if self.store is not None:
            self.store.append_message(msg)
        self._save()

    def _save(self) -> None:
        if self.store is not None:
            self.store.save_snapshot(self.snapshot())

    def save(self) -> None:
        """현재 상태를 저장한다 (자동 저장 외에 따로 남겨야 할 때)."""
        self._save()

    def snapshot(self) -> SessionSnapshot:
        snap = SessionSnapshot(
            config=self.config,
            state=self.state,
            turn=self.turn,
            max_turns=self.max_turns,
            next_index=self._next_index,
            cursors=dict(self._cursor),
            spoken=sorted(self._spoken),
            interrupted=sorted(self._interrupted),
            forced_next=self._forced_next,
            mod_cursor=self._mod_cursor,
            agent_sessions={a.name: getattr(a, "session_id", None) for a in self.agents},
            moderator_session=getattr(self.moderator, "session_id", None),
            usage=dict(self.usage),
            end_vote=self.end_vote.to_state() if self.end_vote is not None else {},
            message_count=len(self.transcript),
        )
        if self._created_at is None:
            self._created_at = snap.created_at
        snap.created_at = self._created_at
        return snap

    def restore(self, snap: SessionSnapshot, messages: list[Message]) -> None:
        """저장된 진행 상태로 되돌린다. 진행 중이던 대화는 PAUSED로, 종료된 대화도 이어갈 수
        있게 PAUSED로 연다 (plan.md 2장: ENDED → 이어하기 → PAUSED)."""
        if self.state != ConversationState.IDLE or self.transcript:
            raise RuntimeError("새 오케스트레이터에서만 복원할 수 있습니다.")
        self.transcript = list(messages)
        self.turn = snap.turn
        self.max_turns = snap.max_turns
        self._next_index = snap.next_index % len(self.agents)
        for name in self._cursor:
            self._cursor[name] = min(snap.cursors.get(name, 0), len(self.transcript))
        self._spoken = set(snap.spoken) & set(self.config.agent_names)
        self._interrupted = set(snap.interrupted) & set(self.config.agent_names)
        self._forced_next = snap.forced_next if snap.forced_next in self._cursor else None
        self._mod_cursor = min(snap.mod_cursor, len(self.transcript))
        self.usage = dict(snap.usage)
        self._created_at = snap.created_at
        if self.end_vote is not None:
            self.end_vote.restore(snap.end_vote)
            if snap.state == ConversationState.ENDED:
                self.end_vote.reopen()
        if snap.state != ConversationState.IDLE:
            self.state = ConversationState.PAUSED
        self._save()

    async def close(self) -> None:
        await self._interrupt_current("stop")
        for agent in self.agents:
            try:
                await agent.disconnect()
            except Exception:
                log.exception("에이전트 %s 연결 종료 실패", agent.name)
        if self.moderator is not None:
            try:
                await self.moderator.disconnect()
            except Exception:
                log.exception("진행자 연결 종료 실패")
        if self.store is not None:
            # 마지막 세션 ID까지 남기고, 밀린 쓰기를 모두 끝낸다
            self._save()
            await self.store.aflush()
            self.store.close()
