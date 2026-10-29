"""활동 카드: 말풍선 아래 접힌 한 줄 요약, 클릭하면 도구별 상세 (plan.md 6.3)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from app.core.activity import ToolUse, detail_lines, summarize


class ActivityCard(QWidget):
    def __init__(self, uses: list[ToolUse] | None = None) -> None:
        super().__init__()
        self._uses: list[ToolUse] = []
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle.setAutoRaise(True)
        self.toggle.setStyleSheet("color: gray; font-size: 11px; text-align: left;")
        self.toggle.toggled.connect(self._on_toggled)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setStyleSheet("color: gray; font-size: 11px; margin-left: 14px;")
        self.details.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details)
        self.set_uses(uses or [])

    @property
    def uses(self) -> list[ToolUse]:
        return list(self._uses)

    def set_uses(self, uses: list[ToolUse]) -> None:
        self._uses = list(uses)
        self._refresh()

    def add(self, use: ToolUse) -> None:
        self._uses.append(use)
        self._refresh()

    @property
    def summary(self) -> str:
        return summarize(self._uses)

    def _refresh(self) -> None:
        self.setVisible(bool(self._uses))
        arrow = "▾" if self.toggle.isChecked() else "▸"
        self.toggle.setText(f"{arrow} {self.summary}")
        self.details.setText("\n".join(detail_lines(self._uses)))

    def _on_toggled(self, checked: bool) -> None:
        self.details.setVisible(checked)
        self._refresh()
