"""사용자 메시지 입력창: Enter로 보내고 Shift+Enter로 줄을 바꾼다."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QPlainTextEdit

INPUT_HEIGHT = 56


class MessageInput(QPlainTextEdit):
    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setPlaceholderText(
            "메시지 입력 — Enter 보내기 · Shift+Enter 줄바꿈 · @이름으로 다음 발언자 지목"
        )
        self.setFixedHeight(INPUT_HEIGHT)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt 메서드 이름
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submit()
            return
        super().keyPressEvent(event)

    def submit(self) -> None:
        text = self.toPlainText().strip()
        if text:
            self.clear()
            self.submitted.emit(text)
