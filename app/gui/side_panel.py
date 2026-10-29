"""오른쪽 사이드 패널: 참가자 · 개인 폴더 트리 · 열람 권한 표 · 토큰 사용량 (plan.md 6.1)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.moderator import MODERATOR_USAGE_KEY
from app.core.orchestrator import Orchestrator
from app.gui.theme import avatar_pixmap

PANEL_WIDTH = 260
MODEL_LABELS = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}


def _tokens(i: int, o: int) -> str:
    return f"{i + o:,}"


def _readonly_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class SidePanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(200)
        self._agents_dir: Path | None = None

        # 참가자
        self.participants = _readonly_table(["참가자", "모델", "토큰"])
        box_people = QGroupBox("참가자")
        QVBoxLayout(box_people).addWidget(self.participants)

        # 개인 폴더 (읽기 전용 보기 — 파일 실행을 막기 위해 더블클릭으로 열지 않는다)
        self.fs_model = QFileSystemModel(self)
        self.fs_model.setReadOnly(True)
        self.fs_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.open_folder_btn = QPushButton("탐색기에서 열기")
        self.open_folder_btn.clicked.connect(self._open_folder)
        box_files = QGroupBox("개인 폴더")
        files_layout = QVBoxLayout(box_files)
        files_layout.addWidget(self.tree)
        files_layout.addWidget(self.open_folder_btn)

        # 열람 권한
        self.access = _readonly_table(["읽는 사람", "폴더 주인", "상태"])
        box_access = QGroupBox("열람 권한")
        QVBoxLayout(box_access).addWidget(self.access)

        # 토큰
        self.total_label = QLabel("")
        self.total_label.setWordWrap(True)
        box_usage = QGroupBox("토큰 사용량")
        QVBoxLayout(box_usage).addWidget(self.total_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box_people, 2)
        layout.addWidget(box_files, 3)
        layout.addWidget(box_access, 2)
        layout.addWidget(box_usage, 0)
        self.clear()

    # ── 대화 바꾸기 ──────────────────────────────────
    def clear(self) -> None:
        self._agents_dir = None
        self.participants.setRowCount(0)
        self.access.setRowCount(0)
        self.total_label.setText("대화가 없습니다.")
        self.tree.setRootIndex(self.fs_model.index(""))
        self.tree.setEnabled(False)
        self.open_folder_btn.setEnabled(False)

    def set_conversation(self, orch: Orchestrator, session_dir: Path) -> None:
        self._agents_dir = session_dir / "agents"
        self.fs_model.setRootPath(str(self._agents_dir))
        self.tree.setRootIndex(self.fs_model.index(str(self._agents_dir)))
        self.tree.setEnabled(True)
        self.open_folder_btn.setEnabled(True)
        self.refresh(orch)

    # ── 갱신 ─────────────────────────────────────────
    def refresh(self, orch: Orchestrator) -> None:
        self._refresh_participants(orch)
        self._refresh_access(orch)
        total_in = sum(i for i, _ in orch.usage.values())
        total_out = sum(o for _, o in orch.usage.values())
        self.total_label.setText(
            f"합계 {total_in + total_out:,}\n입력 {total_in:,} · 출력 {total_out:,}"
        )

    def _refresh_participants(self, orch: Orchestrator) -> None:
        rows: list[tuple[str, str, str, str | None]] = []
        speaking = orch.speaking
        for a in orch.config.agents:
            label = f"{a.name}  ● 발언 중" if a.name == speaking else a.name
            i, o = orch.usage.get(a.name, (0, 0))
            rows.append((label, MODEL_LABELS.get(a.model, a.model), _tokens(i, o), a.color))
        if orch.moderator is not None:
            i, o = orch.usage.get(MODERATOR_USAGE_KEY, (0, 0))
            model = MODEL_LABELS.get(orch.config.moderator_model, orch.config.moderator_model)
            rows.append(("(진행자)", model, _tokens(i, o), None))
        self.participants.setRowCount(len(rows))
        for r, (name, model, tokens, color) in enumerate(rows):
            item = QTableWidgetItem(name)
            if color is not None:
                item.setIcon(QIcon(avatar_pixmap(" ", color, None, 12)))
                if name.endswith("발언 중"):
                    item.setForeground(QColor(color))
            self.participants.setItem(r, 0, item)
            self.participants.setItem(r, 1, QTableWidgetItem(model))
            self.participants.setItem(r, 2, QTableWidgetItem(tokens))

    def _refresh_access(self, orch: Orchestrator) -> None:
        rows: list[tuple[str, str, str]] = []
        if orch.acl is not None:
            rows += [(g.reader, g.owner, "읽기 허가") for g in orch.acl.grants]
            rows += [
                (r.requester, r.target, f"요청 #{r.id} 대기")
                for r in orch.acl.requests
                if r.status.value == "pending"
            ]
        self.access.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                self.access.setItem(r, c, QTableWidgetItem(text))

    def _open_folder(self) -> None:
        if self._agents_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._agents_dir)))

    def rows(self, table: QTableWidget) -> list[list[str]]:
        """테스트·디버그용: 표 내용을 글자로."""
        return [
            [table.item(r, c).text() for c in range(table.columnCount())]
            for r in range(table.rowCount())
        ]
