"""엔진 이벤트 → Qt Signal 어댑터. app/core는 Qt를 모르므로 여기서만 연결한다."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from app.core.events import Event


class EngineBridge(QObject):
    event = Signal(object)

    def emit(self, e: Event) -> None:
        """Orchestrator의 EventSink로 넘긴다."""
        self.event.emit(e)
