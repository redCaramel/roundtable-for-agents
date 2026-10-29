"""메인 창: 헤더(주제·턴) + 채팅 + 입력창 + 제어 버튼.

엔진은 qasync 위에서 asyncio 태스크로 돈다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.access_control import AccessControl
from app.core.events import Event, StateChanged, TurnCompleted, TurnStarted, UsageUpdated
from app.core.models import ConversationState, SessionConfig
from app.core.orchestrator import Orchestrator
from app.core.session_factory import create_agent_runners
from app.core.storage import new_session_dir
from app.gui.chat_view import ChatView
from app.gui.engine_bridge import EngineBridge
from app.gui.message_input import MessageInput
from app.gui.setup_dialog import SetupDialog

log = logging.getLogger(__name__)

ADD_TURNS = 10


class MainWindow(QMainWindow):
    def __init__(self, conversations_root: Path) -> None:
        super().__init__()
        self.setWindowTitle("Roundtable for Agents")
        self.resize(900, 760)
        self.conversations_root = conversations_root
        self.orch: Orchestrator | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed_cleanly = False
        self._speaking: str | None = None
        # 버튼 연타로 같은 제어가 겹치지 않게 한다 (중단은 최대 수 초 걸릴 수 있다)
        self._control_busy = False
        self._control_future: asyncio.Future[None] | None = None

        self.bridge = EngineBridge()
        self.bridge.event.connect(self._on_event)

        self.topic_label = QLabel("새 대화를 시작하세요 (파일 → 새 대화)")
        self.topic_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.turn_label = QLabel("")
        header = QHBoxLayout()
        header.addWidget(self.topic_label, 1)
        header.addWidget(self.turn_label)

        self.chat = ChatView()

        self.input = MessageInput()
        self.input.submitted.connect(self._on_send)
        self.send_btn = QPushButton("보내기")
        self.send_btn.clicked.connect(self.input.submit)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_btn)

        self.start_btn = QPushButton("▶ 시작")
        self.start_btn.clicked.connect(self._on_start)
        self.pause_btn = QPushButton("⏸ 일시정지")
        self.pause_btn.clicked.connect(self._on_pause)
        self.stop_btn = QPushButton("■ 종료")
        self.stop_btn.clicked.connect(self._on_stop)
        self.add_turns_btn = QPushButton(f"턴 +{ADD_TURNS}")
        self.add_turns_btn.clicked.connect(self._on_add_turns)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray;")
        controls = QHBoxLayout()
        for btn in (self.start_btn, self.pause_btn, self.stop_btn, self.add_turns_btn):
            controls.addWidget(btn)
        controls.addStretch(1)
        controls.addWidget(self.status_label)

        root = QVBoxLayout()
        root.addLayout(header)
        root.addWidget(self.chat, 1)
        root.addLayout(input_row)
        root.addLayout(controls)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        new_action = QAction("새 대화…", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.open_setup)
        self.menuBar().addMenu("파일").addAction(new_action)
        self._refresh_controls()

    # ── 대화 만들기 ──────────────────────────────────
    def open_setup(self) -> None:
        if self._is_running():
            QMessageBox.information(
                self,
                "진행 중",
                "대화가 진행 중입니다. 일시정지하거나 종료한 뒤 새 대화를 시작하세요.",
            )
            return
        dialog = SetupDialog(self)
        dialog.accepted.connect(lambda: self._begin(dialog.result_config))
        dialog.open()

    def _begin(self, config: SessionConfig | None) -> None:
        if config is None:
            return
        asyncio.ensure_future(self.start_conversation(config))

    async def start_conversation(self, config: SessionConfig, autostart: bool = True) -> None:
        if self.orch is not None:
            await self.orch.close()
        session_dir = new_session_dir(self.conversations_root, config.topic)
        acl = AccessControl(config.agent_names, session_dir / "acl.json")
        runners = create_agent_runners(
            config, session_dir, acl=acl, current_turn=lambda: self.orch.turn if self.orch else 0
        )
        self.orch = Orchestrator(config, runners, self.bridge.emit, acl=acl)
        self.chat.clear()
        self.chat.set_agents(config.agents)
        first_line = config.topic.strip().splitlines()[0]
        self.topic_label.setText(f"주제: {first_line}")
        self.topic_label.setToolTip(config.topic)
        self.status_label.setText(f"폴더: {session_dir.name}")
        self._refresh_controls()
        if autostart:
            self._run()

    # ── 진행 ─────────────────────────────────────────
    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _run(self) -> None:
        if self.orch is None or self._is_running():
            return
        if self.orch.state == ConversationState.ENDED:
            return
        self._task = asyncio.ensure_future(self.orch.run())
        self._task.add_done_callback(self._on_task_done)
        self._refresh_controls()

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("대화 진행 실패", exc_info=task.exception())
            self.status_label.setText(f"오류: {task.exception()}")
        self._refresh_controls()

    def _on_start(self) -> None:
        if self.orch is None:
            self.open_setup()
        else:
            self._run()

    def _on_add_turns(self) -> None:
        if self.orch is not None:
            self.orch.add_turns(ADD_TURNS)
            self._run()

    # ── 사용자 제어 ──────────────────────────────────
    def _on_send(self, text: str) -> None:
        if self.orch is None or self.orch.state == ConversationState.ENDED:
            return
        asyncio.ensure_future(self._send(text))

    async def _send(self, text: str) -> None:
        assert self.orch is not None
        try:
            await self.orch.send_user_message(text)
        except (ValueError, RuntimeError) as e:
            self.status_label.setText(str(e))
            return
        if self.orch.state == ConversationState.PAUSED:
            self.status_label.setText("메시지를 남겼습니다. ▶ 재개를 누르면 반영됩니다.")

    def _on_pause(self) -> None:
        self._control_future = asyncio.ensure_future(self._control(self._pause_now))

    async def _pause_now(self) -> None:
        if self.orch is not None:
            await self.orch.pause()

    def _on_stop(self) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "대화 종료",
            "대화를 종료할까요? 진행 중인 발언은 버려집니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.button(QMessageBox.StandardButton.Yes).setText("종료")
        box.button(QMessageBox.StandardButton.No).setText("취소")
        box.accepted.connect(lambda: asyncio.ensure_future(self._control(self.stop_now)))
        box.open()

    async def stop_now(self) -> None:
        if self.orch is not None:
            await self.orch.stop()

    async def _control(self, action) -> None:  # noqa: ANN001
        if self._control_busy:
            return
        self._control_busy = True
        self._refresh_controls()
        try:
            await action()
        finally:
            self._control_busy = False
            self._refresh_controls()

    # ── 화면 갱신 ────────────────────────────────────
    def _refresh_controls(self) -> None:
        running = self._is_running()
        orch = self.orch
        ended = orch is not None and orch.state == ConversationState.ENDED
        at_limit = orch is not None and orch.turn >= orch.max_turns
        busy = self._control_busy
        self.start_btn.setText("▶ 시작" if orch is None or orch.turn == 0 else "▶ 재개")
        self.start_btn.setEnabled(not running and not at_limit and not ended and not busy)
        self.pause_btn.setEnabled(running and not busy)
        self.stop_btn.setEnabled(orch is not None and not ended and not busy)
        self.add_turns_btn.setVisible(orch is not None and at_limit and not running and not ended)
        self.input.setEnabled(orch is not None and not ended)
        self.send_btn.setEnabled(orch is not None and not ended)
        if orch is not None:
            if ended:
                status = " · 종료됨"
            elif running and self._speaking:
                status = f" · {self._speaking} 발언 중"
            elif not running and orch.turn > 0:
                status = " · 일시정지"
            else:
                status = ""
            self.turn_label.setText(f"턴 {orch.turn}/{orch.max_turns}{status}")

    def _on_event(self, e: Event) -> None:
        self.chat.handle_event(e)
        if isinstance(e, TurnStarted):
            self._speaking = e.agent
        elif isinstance(e, TurnCompleted):
            self._speaking = None
        elif isinstance(e, UsageUpdated) and self.orch is not None:
            total_in = sum(i for i, _ in self.orch.usage.values())
            total_out = sum(o for _, o in self.orch.usage.values())
            self.status_label.setText(f"토큰 입력 {total_in:,} · 출력 {total_out:,}")
        elif isinstance(e, StateChanged) and e.state != ConversationState.RUNNING:
            self._speaking = None
        self._refresh_controls()

    # ── 창 닫기 ──────────────────────────────────────
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt 메서드 이름
        if self._closed_cleanly or self.orch is None:
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.status_label.setText("종료 중…")
            asyncio.ensure_future(self._shutdown_and_close())

    async def _shutdown_and_close(self) -> None:
        try:
            await self.shutdown()
        finally:
            self._closed_cleanly = True
            self.close()

    async def shutdown(self) -> None:
        """진행 중인 발언을 중단하고 모든 에이전트 연결을 닫는다."""
        if self.orch is not None:
            await self.orch.pause()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
        if self.orch is not None:
            await self.orch.close()
