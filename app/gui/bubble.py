"""말풍선 위젯: 에이전트(왼쪽·고유 색·아바타), 사용자(오른쪽), 시스템(가운데 회색)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.core.activity import ToolUse
from app.core.models import AgentConfig
from app.gui.activity_card import ActivityCard
from app.gui.theme import AVATAR_SIZE, USER_COLOR, avatar_pixmap, tint

BUBBLE_MAX_WIDTH = 620
STREAMING_PLACEHOLDER = "…"


def _text_label(text: str, markdown: bool = True) -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.MarkdownText if markdown else Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
    )
    label.setOpenExternalLinks(True)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setText(text)
    return label


class AgentBubble(QWidget):
    def __init__(
        self,
        agent: AgentConfig,
        text: str = "",
        streaming: bool = False,
        activities: list[ToolUse] | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self._raw = text
        self.streaming = streaming

        avatar = QLabel()
        avatar.setPixmap(avatar_pixmap(agent.name, agent.color, agent.avatar_path))
        avatar.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        avatar.setAlignment(Qt.AlignmentFlag.AlignTop)

        name = QLabel(agent.name)
        name.setStyleSheet(f"color: {agent.color}; font-weight: bold;")

        self.body = _text_label(text or STREAMING_PLACEHOLDER)
        self.frame = QFrame()
        self.frame.setObjectName("bubble")
        self.frame.setMaximumWidth(BUBBLE_MAX_WIDTH)
        self.frame.setStyleSheet(
            f"#bubble {{ background: {tint(agent.color, 38)}; "
            f"border: 1px solid {tint(agent.color, 140)}; border-radius: 10px; }}"
        )
        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.addWidget(self.body)

        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(name)
        col.addWidget(self.frame)
        self.card = ActivityCard(activities)
        col.addWidget(self.card)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(col, 1)
        row.addStretch(0)
        self._set_streaming_style()

    def _set_streaming_style(self) -> None:
        self.body.setEnabled(not self.streaming)  # 스트리밍 중엔 흐리게

    def append(self, delta: str) -> None:
        self._raw += delta
        self.body.setText(self._raw)

    def finish(self, final_text: str, activities: list[ToolUse] | None = None) -> None:
        """확정 텍스트로 교체한다 (엔진이 이름 접두 등을 정리한 버전)."""
        self.streaming = False
        self._raw = final_text
        self.body.setText(final_text)
        if activities is not None:
            self.card.set_uses(activities)
        self._set_streaming_style()

    @property
    def text(self) -> str:
        return self._raw


class UserBubble(QWidget):
    def __init__(self, text: str) -> None:
        super().__init__()
        body = _text_label(text)
        frame = QFrame()
        frame.setObjectName("bubble")
        frame.setMaximumWidth(BUBBLE_MAX_WIDTH)
        frame.setStyleSheet(
            f"#bubble {{ background: {tint(USER_COLOR, 50)}; border-radius: 10px; }}"
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.addWidget(body)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addStretch(1)
        row.addWidget(frame)


class SystemLine(QWidget):
    def __init__(self, text: str) -> None:
        super().__init__()
        label = _text_label(f"[시스템] {text}", markdown=False)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: gray; font-size: 11px;")
        row = QHBoxLayout(self)
        row.setContentsMargins(40, 2, 40, 2)
        row.addWidget(label)
