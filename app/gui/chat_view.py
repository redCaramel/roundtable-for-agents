"""말풍선 목록 + 스트리밍 갱신."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.core.activity import make_tool_use
from app.core.events import (
    Event,
    StateChanged,
    SystemNotice,
    TextDelta,
    ToolActivity,
    TurnCompleted,
    TurnInterrupted,
    TurnStarted,
    UserMessageAdded,
)
from app.core.models import AgentConfig, ConversationState, Message
from app.gui.bubble import AgentBubble, SystemLine, UserBubble

INTERRUPT_REASONS = {
    "user": "사용자 개입으로 중단",
    "pause": "일시정지로 중단",
    "stop": "종료로 중단",
}


class ChatView(QScrollArea):
    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(4, 8, 4, 8)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        self.setWidget(self._content)
        self._agents: dict[str, AgentConfig] = {}
        self._streaming: AgentBubble | None = None

    def set_agents(self, agents: list[AgentConfig]) -> None:
        self._agents = {a.name: a for a in agents}

    def clear(self) -> None:
        self._streaming = None
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @property
    def bubble_count(self) -> int:
        return self._layout.count() - 1

    def widgets(self) -> list[QWidget]:
        return [self._layout.itemAt(i).widget() for i in range(self.bubble_count)]

    # ── 추가 ─────────────────────────────────────────
    def _add(self, w: QWidget) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 40
        self._layout.insertWidget(self._layout.count() - 1, w)
        if at_bottom:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        # 레이아웃이 크기를 다시 잡은 뒤에 내려야 끝까지 내려간다.
        QTimer.singleShot(
            0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        )

    def add_message(self, msg: Message) -> None:
        if msg.kind == "agent" and msg.speaker in self._agents:
            self._add(AgentBubble(self._agents[msg.speaker], msg.text, activities=msg.activities))
        elif msg.kind == "user":
            self._add(UserBubble(msg.text))
        else:
            self._add(SystemLine(msg.text))

    # ── 엔진 이벤트 ──────────────────────────────────
    def handle_event(self, e: Event) -> None:
        if isinstance(e, TurnStarted):
            self._discard_streaming()
            agent = self._agents.get(e.agent)
            if agent is not None:
                self._streaming = AgentBubble(agent, streaming=True)
                self._add(self._streaming)
        elif isinstance(e, TextDelta):
            if self._streaming is not None and self._streaming.agent.name == e.agent:
                self._streaming.append(e.text)
                self._scroll_if_near_bottom()
        elif isinstance(e, ToolActivity):
            # 발언 중에도 활동 카드를 바로바로 채운다
            if self._streaming is not None and self._streaming.agent.name == e.agent:
                self._streaming.card.add(make_tool_use(e.tool, e.tool_input, e.denied, e.reason))
        elif isinstance(e, TurnCompleted):
            if self._streaming is not None and self._streaming.agent.name == e.message.speaker:
                self._streaming.finish(e.message.text, e.message.activities)
                self._streaming = None
            else:
                self.add_message(e.message)
        elif isinstance(e, TurnInterrupted):
            self._discard_streaming()
            why = INTERRUPT_REASONS.get(e.reason, "중단")
            self._add(SystemLine(f"({e.agent}의 발언이 {why}되어 버려졌습니다)"))
        elif isinstance(e, UserMessageAdded):
            self._add(UserBubble(e.message.text))
        elif isinstance(e, SystemNotice):
            self._add(SystemLine(e.message.text))
        elif isinstance(e, StateChanged) and e.state != ConversationState.RUNNING:
            # 확정되지 못한 발언(오류·중단)은 폐기한다 (plan.md 6.2).
            self._discard_streaming()

    def _discard_streaming(self) -> None:
        if self._streaming is not None:
            self._layout.removeWidget(self._streaming)
            self._streaming.deleteLater()
            self._streaming = None

    def _scroll_if_near_bottom(self) -> None:
        bar = self.verticalScrollBar()
        if bar.value() >= bar.maximum() - 80:
            self._scroll_to_bottom()
