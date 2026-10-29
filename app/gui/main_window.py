"""메인 창: 대화 목록 | 헤더(주제·턴) + 채팅 + 입력창 + 제어 버튼 | 사이드 패널.

엔진은 qasync 위에서 asyncio 태스크로 돈다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.core.events import (
    Event,
    ModeratorDecided,
    StateChanged,
    SystemNotice,
    TurnCompleted,
    TurnStarted,
    UsageUpdated,
)
from app.core.models import ConversationState, SessionConfig
from app.core.orchestrator import Orchestrator
from app.core.session_factory import build_orchestrator, load_orchestrator
from app.core.storage import list_sessions, new_session_dir
from app.gui.chat_view import ChatView
from app.gui.conversation_list import LIST_WIDTH, ConversationList, shorten
from app.gui.engine_bridge import EngineBridge
from app.gui.message_input import MessageInput
from app.gui.setup_dialog import SetupDialog
from app.gui.side_panel import PANEL_WIDTH, SidePanel

log = logging.getLogger(__name__)

ADD_TURNS = 10
HEADER_TOPIC_CHARS = 50


class MainWindow(QMainWindow):
    def __init__(self, conversations_root: Path, presets_root: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Roundtable for Agents")
        self.resize(1380, 800)
        self.conversations_root = conversations_root
        self.presets_root = presets_root or conversations_root.parent / "presets"
        self.orch: Orchestrator | None = None
        self.session_dir: Path | None = None
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
        self.moderator_label = QLabel("")
        self.moderator_label.setStyleSheet("color: gray;")
        controls.addWidget(self.moderator_label)
        controls.addWidget(self.status_label)

        root = QVBoxLayout()
        root.addLayout(header)
        root.addWidget(self.chat, 1)
        root.addLayout(input_row)
        root.addLayout(controls)
        main = QWidget()
        main.setLayout(root)

        self.conversations = ConversationList()
        self.conversations.new_requested.connect(self.open_setup)
        self.conversations.open_requested.connect(self._on_open_requested)
        self.side = SidePanel()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.conversations)
        splitter.addWidget(main)
        splitter.addWidget(self.side)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([LIST_WIDTH, self.width() - LIST_WIDTH - PANEL_WIDTH, PANEL_WIDTH])
        self.setCentralWidget(splitter)

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
        dialog = SetupDialog(self, self.presets_root)
        dialog.accepted.connect(lambda: self._begin(dialog.result_config))
        dialog.open()

    def _begin(self, config: SessionConfig | None) -> None:
        if config is None:
            return
        asyncio.ensure_future(self.start_conversation(config))

    async def start_conversation(self, config: SessionConfig, autostart: bool = True) -> None:
        await self._close_current()
        session_dir = new_session_dir(self.conversations_root, config.topic)
        self.orch = build_orchestrator(config, session_dir, self.bridge.emit)
        self._show_conversation(session_dir)
        self.status_label.setText(f"폴더: {session_dir.name}")
        await self.refresh_list()
        if autostart:
            self._run()

    # ── 저장된 대화 이어하기 (plan.md 8장) ───────────
    def _on_open_requested(self, path: Path) -> None:
        if self.session_dir is not None and path.resolve() == self.session_dir.resolve():
            return
        if self._is_running() or self._control_busy:
            QMessageBox.information(
                self,
                "진행 중",
                "대화가 진행 중입니다. 일시정지하거나 종료한 뒤 다른 대화를 여세요.",
            )
            return
        asyncio.ensure_future(self.open_conversation(path))

    async def open_conversation(self, path: Path) -> None:
        """저장된 대화를 불러와 화면에 복원한다. 이어가려면 ▶ 재개를 누른다."""
        await self._close_current()
        try:
            self.orch = await load_orchestrator(path, self.bridge.emit)
        except (OSError, ValueError) as e:
            log.exception("대화 불러오기 실패: %s", path)
            QMessageBox.warning(self, "불러오기 실패", f"대화를 불러오지 못했습니다.\n{e}")
            await self.refresh_list()
            return
        self._show_conversation(path)
        for msg in self.orch.transcript:
            self.chat.add_message(msg)
        self.status_label.setText("불러왔습니다. ▶ 재개를 누르면 이어서 진행합니다.")
        await self.refresh_list()

    async def _close_current(self) -> None:
        if self.orch is not None:
            await self.orch.close()
        self.orch = None
        self.session_dir = None
        self._speaking = None
        self.moderator_label.clear()
        self.side.clear()

    def _show_conversation(self, session_dir: Path) -> None:
        assert self.orch is not None
        config = self.orch.config
        self.session_dir = session_dir
        self.chat.clear()
        self.chat.set_agents(config.agents)
        first_line = config.topic.strip().splitlines()[0]
        self.topic_label.setText(f"주제: {shorten(first_line, HEADER_TOPIC_CHARS)}")
        self.topic_label.setToolTip(config.topic)
        self.side.set_conversation(self.orch, session_dir)
        self._refresh_controls()

    async def refresh_list(self) -> None:
        infos = await asyncio.to_thread(list_sessions, self.conversations_root)
        self.conversations.set_sessions(infos, self.session_dir)

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
        elif isinstance(e, ModeratorDecided):
            # 진행자 판단 이유는 채팅에 남기지 않고 상태줄에만 보여준다 (plan.md 4.5)
            who = e.next or "종료"
            prefix = "진행자 판단 실패, 순서대로" if e.fallback else "진행자"
            self.moderator_label.setText(f"{prefix} → {who}")
            self.moderator_label.setToolTip(e.reason)
        elif isinstance(e, StateChanged):
            if e.state != ConversationState.RUNNING:
                self._speaking = None
            # 목록의 상태·턴 표시를 맞춘다 (저장은 쓰기 스레드에서 끝난 뒤 읽는다)
            asyncio.ensure_future(self._refresh_list_after_save())
        if self.orch is not None and isinstance(
            e, TurnStarted | TurnCompleted | UsageUpdated | SystemNotice | StateChanged
        ):
            # 발언자 표시·토큰·열람 권한(변경은 시스템 알림으로 온다)을 맞춘다
            self.side.refresh(self.orch)
        self._refresh_controls()

    async def _refresh_list_after_save(self) -> None:
        if self.orch is not None and self.orch.store is not None:
            await self.orch.store.aflush()
        await self.refresh_list()

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
