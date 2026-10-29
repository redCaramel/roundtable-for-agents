"""새 대화 설정 화면 (plan.md 6.4)."""

from __future__ import annotations

from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.models import (
    DEFAULT_MAX_TURNS,
    MAX_AGENTS,
    MIN_AGENTS,
    AgentConfig,
    EndPolicy,
    SessionConfig,
    TurnMode,
)
from app.gui.theme import AGENT_COLORS, DEFAULT_NAMES, MODELS, avatar_pixmap

TURN_MODES = [(TurnMode.ROUND_ROBIN, "라운드로빈 (순서대로)"), (TurnMode.MODERATOR, "진행자 지목")]
END_POLICIES = [
    (EndPolicy.UNANIMOUS, "전원 동의 시 종료"),
    (EndPolicy.ANY, "1명 제안 즉시 종료"),
    (EndPolicy.MODERATOR, "진행자 선언"),
]


def _combo(items: list[tuple[object, str]], current: object | None = None) -> QComboBox:
    box = QComboBox()
    for value, label in items:
        box.addItem(label, value)
    if current is not None:
        box.setCurrentIndex(max(0, box.findData(current)))
    return box


class AgentCard(QGroupBox):
    """에이전트 1명의 이름 · 색 · 아바타 · 모델 · 개인 지시사항."""

    def __init__(self, index: int) -> None:
        super().__init__(f"에이전트 {index + 1}")
        self._color = AGENT_COLORS[index % len(AGENT_COLORS)]
        self._avatar: str | None = None

        self.name_edit = QLineEdit(DEFAULT_NAMES[index % len(DEFAULT_NAMES)])
        self.name_edit.setMaxLength(20)
        self.name_edit.textChanged.connect(self._refresh_avatar)

        self.color_btn = QPushButton("색 변경")
        self.color_btn.clicked.connect(self._pick_color)
        self.avatar_btn = QPushButton("아바타 이미지…")
        self.avatar_btn.clicked.connect(self._pick_avatar)
        self.avatar_clear = QPushButton("기본")
        self.avatar_clear.setToolTip("이미지 대신 이름 첫 글자 사용")
        self.avatar_clear.clicked.connect(self._clear_avatar)
        self.preview = QLabel()

        self.model_box = _combo([(v, label) for v, label in MODELS], "sonnet")
        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText(
            "이 에이전트만 보는 지시사항 (선택, 다른 에이전트에게 공개되지 않음)"
        )
        self.instructions.setFixedHeight(60)

        look = QHBoxLayout()
        look.addWidget(self.preview)
        look.addWidget(self.color_btn)
        look.addWidget(self.avatar_btn)
        look.addWidget(self.avatar_clear)
        look.addStretch(1)

        form = QFormLayout(self)
        form.addRow("이름", self.name_edit)
        form.addRow("모양", look)
        form.addRow("모델", self.model_box)
        form.addRow("개인 지시사항", self.instructions)
        self._refresh_avatar()

    def _refresh_avatar(self) -> None:
        name = self.name_edit.text().strip() or "?"
        self.preview.setPixmap(avatar_pixmap(name, self._color, self._avatar))
        self.color_btn.setIcon(QIcon(avatar_pixmap(" ", self._color, None, 14)))

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "말풍선 색")
        if c.isValid():
            self._color = c.name()
            self._refresh_avatar()

    def _pick_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "아바타 이미지", "", "이미지 (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        if path:
            self._avatar = path
            self._refresh_avatar()

    def _clear_avatar(self) -> None:
        self._avatar = None
        self._refresh_avatar()

    def to_config(self) -> AgentConfig:
        return AgentConfig(
            name=self.name_edit.text(),
            model=self.model_box.currentData(),
            color=self._color,
            avatar_path=self._avatar,
            instructions=self.instructions.toPlainText().strip(),
        )


class SetupDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("새 대화 설정")
        self.resize(640, 760)
        self.result_config: SessionConfig | None = None

        self.topic = QPlainTextEdit()
        self.topic.setPlaceholderText(
            "주제나 규칙을 자유롭게 적습니다.\n"
            "예) 고양이와 강아지 중 어느 쪽이 반려동물로 나은지 토론한다."
        )
        self.topic.setFixedHeight(90)

        self.count = QSpinBox()
        self.count.setRange(MIN_AGENTS, MAX_AGENTS)
        self.count.setValue(2)
        self.count.valueChanged.connect(self._sync_cards)

        self.turn_mode = _combo(TURN_MODES)
        self.turn_mode.setToolTip(
            "진행자 지목: 대화에 끼지 않는 진행자 에이전트가 매번 다음 발언자를 고릅니다."
        )
        self.moderator_model = _combo([(v, label) for v, label in MODELS], "haiku")
        self.max_turns = QSpinBox()
        self.max_turns.setRange(1, 500)
        self.max_turns.setValue(DEFAULT_MAX_TURNS)
        self.end_policy = _combo(END_POLICIES)
        self.turn_mode.currentIndexChanged.connect(self._sync_moderator_options)
        self._sync_moderator_options()

        settings = QFormLayout()
        settings.addRow("주제 / 규칙", self.topic)
        settings.addRow("에이전트 수", self.count)
        settings.addRow("발언 순서", self.turn_mode)
        settings.addRow("진행자 모델", self.moderator_model)
        settings.addRow("최대 턴", self.max_turns)
        settings.addRow("종료 정책", self.end_policy)

        self._cards_box = QVBoxLayout()
        self._cards: list[AgentCard] = []
        cards_widget = QWidget()
        cards_widget.setLayout(self._cards_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_widget)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("대화 시작")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(settings)
        root.addWidget(QLabel("참가자"), 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(scroll, 1)
        root.addWidget(buttons)
        self._sync_cards()

    @property
    def cards(self) -> list[AgentCard]:
        return list(self._cards)

    def _sync_moderator_options(self) -> None:
        """진행자 모델과 '진행자 선언' 종료는 진행자 지목 모드에서만 고를 수 있다."""
        moderated = self.turn_mode.currentData() == TurnMode.MODERATOR
        self.moderator_model.setEnabled(moderated)
        declare_index = self.end_policy.findData(EndPolicy.MODERATOR)
        self.end_policy.model().item(declare_index).setEnabled(moderated)
        if not moderated and self.end_policy.currentIndex() == declare_index:
            self.end_policy.setCurrentIndex(self.end_policy.findData(EndPolicy.UNANIMOUS))

    def _sync_cards(self) -> None:
        n = self.count.value()
        while len(self._cards) < n:
            card = AgentCard(len(self._cards))
            self._cards.append(card)
            self._cards_box.addWidget(card)
        while len(self._cards) > n:
            card = self._cards.pop()
            self._cards_box.removeWidget(card)
            card.deleteLater()

    def build_config(self) -> SessionConfig:
        """입력값으로 설정을 만든다. 잘못되면 ValueError(메시지는 사용자용)."""
        topic = self.topic.toPlainText().strip()
        if not topic:
            raise ValueError("주제/규칙을 입력하세요.")
        try:
            return SessionConfig(
                topic=topic,
                agents=[c.to_config() for c in self._cards],
                turn_mode=self.turn_mode.currentData(),
                max_turns=self.max_turns.value(),
                end_policy=self.end_policy.currentData(),
                moderator_model=self.moderator_model.currentData(),
            )
        except ValidationError as e:
            msgs = [err["msg"].removeprefix("Value error, ") for err in e.errors()]
            raise ValueError("\n".join(msgs)) from None

    def _accept(self) -> None:
        try:
            self.result_config = self.build_config()
        except ValueError as e:
            QMessageBox.warning(self, "설정 확인", str(e))
            return
        self.accept()
