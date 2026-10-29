"""왼쪽 대화 목록: 저장된 대화를 최근 순으로 보여주고, 골라서 이어하게 한다 (plan.md 6.1)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from app.core.models import ConversationState
from app.core.storage import SessionInfo

STATE_TEXT = {
    ConversationState.IDLE: "시작 전",
    ConversationState.RUNNING: "진행 중",
    ConversationState.PAUSED: "일시정지",
    ConversationState.ENDED: "종료",
}
LIST_WIDTH = 220
TOPIC_CHARS = 18


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(info: SessionInfo) -> str:
    """두 줄: 주제(한 줄로 줄임) / 상태 · 턴 · 마지막 저장 시각. 전체 주제는 툴팁에 있다."""
    when = info.updated_at.strftime("%m/%d %H:%M")
    return (
        f"{shorten(info.topic or '(주제 없음)', TOPIC_CHARS)}\n"
        f"{STATE_TEXT[info.state]} · 턴 {info.turn}/{info.max_turns} · {when}"
    )


class ConversationList(QWidget):
    new_requested = Signal()
    open_requested = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(160)
        self.resize(LIST_WIDTH, self.height())
        self.new_btn = QPushButton("+ 새 대화")
        self.new_btn.clicked.connect(self.new_requested.emit)
        self.list = QListWidget()
        # 줄바꿈하면 긴 주제가 두 번째 줄(상태·턴)을 밀어내므로 끈다
        self.list.setWordWrap(False)
        self.list.setToolTip("더블클릭하면 그 대화를 불러와 이어갈 수 있습니다.")
        self.list.itemActivated.connect(self._on_activated)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.new_btn)
        layout.addWidget(self.list, 1)

    def set_sessions(self, infos: list[SessionInfo], current: Path | None = None) -> None:
        self.list.clear()
        for info in infos:
            item = QListWidgetItem(describe(info))
            item.setData(Qt.ItemDataRole.UserRole, str(info.path))
            item.setToolTip(f"{info.topic}\n참가자: {', '.join(info.agent_names)}")
            # 두 줄(주제 / 상태·턴)이 다 보이게 높이를 정한다 (기본은 한 줄로 잘린다)
            item.setSizeHint(QSize(0, self.list.fontMetrics().lineSpacing() * 2 + 10))
            self.list.addItem(item)
            if current is not None and info.path.resolve() == current.resolve():
                item.setSelected(True)
                font = item.font()
                font.setBold(True)
                item.setFont(font)

    def paths(self) -> list[Path]:
        return [
            Path(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())
        ]

    def _on_activated(self, item: QListWidgetItem) -> None:
        self.open_requested.emit(Path(item.data(Qt.ItemDataRole.UserRole)))
