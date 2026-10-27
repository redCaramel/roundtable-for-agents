"""GUI 위젯 테스트 (offscreen). 실제 에이전트 대신 가짜 에이전트를 쓴다."""

import asyncio

import pytest
import qasync
from PySide6.QtWidgets import QApplication

from app.core.events import StateChanged, SystemNotice, TextDelta, TurnCompleted, TurnStarted
from app.core.models import AgentConfig, ConversationState, Message, SessionConfig
from app.gui import main_window
from app.gui.bubble import AgentBubble, SystemLine, UserBubble
from app.gui.chat_view import ChatView
from app.gui.main_window import MainWindow
from app.gui.setup_dialog import SetupDialog
from tests.fakes import FakeAgent


@pytest.fixture(scope="module")
def qloop():
    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    yield loop
    asyncio.set_event_loop(None)
    loop.close()


AGENTS = [AgentConfig(name="철수", color="#4a90d9"), AgentConfig(name="영희", color="#e07b39")]


# ── 설정 화면 ──────────────────────────────────────
def test_setup_dialog_builds_config(qloop):
    d = SetupDialog()
    assert len(d.cards) == 2
    d.count.setValue(4)
    assert len(d.cards) == 4
    d.count.setValue(3)
    assert len(d.cards) == 3
    d.topic.setPlainText("점심 메뉴 정하기\n세부 규칙")
    d.cards[0].model_box.setCurrentIndex(d.cards[0].model_box.findData("haiku"))
    d.cards[1].instructions.setPlainText("비밀 지시")
    d.max_turns.setValue(12)
    config = d.build_config()
    assert config.agent_names == ["철수", "영희", "민수"]
    assert config.agents[0].model == "haiku"
    assert config.agents[1].instructions == "비밀 지시"
    assert config.max_turns == 12
    assert len({a.color for a in config.agents}) == 3  # 기본 색이 서로 다르다


def test_setup_dialog_validation_messages(qloop):
    d = SetupDialog()
    with pytest.raises(ValueError, match="주제"):
        d.build_config()
    d.topic.setPlainText("주제")
    d.cards[1].name_edit.setText("철수")
    with pytest.raises(ValueError, match="중복"):
        d.build_config()
    d.cards[1].name_edit.setText("  ")
    with pytest.raises(ValueError, match="비어"):
        d.build_config()


# ── 채팅 화면 ──────────────────────────────────────
def test_chat_view_streams_then_finalizes(qloop):
    view = ChatView()
    view.set_agents(AGENTS)
    view.handle_event(TurnStarted("철수", 1))
    bubble = view.widgets()[0]
    assert isinstance(bubble, AgentBubble) and bubble.streaming
    view.handle_event(TextDelta("철수", "철수: 안녕"))
    view.handle_event(TextDelta("철수", "하세요"))
    assert bubble.text == "철수: 안녕하세요"
    final = Message(kind="agent", speaker="철수", text="안녕하세요", turn=1)
    view.handle_event(TurnCompleted(final))
    assert not bubble.streaming and bubble.text == "안녕하세요"  # 엔진이 정리한 텍스트로 교체
    assert view.bubble_count == 1


def test_chat_view_discards_unfinished_turn(qloop):
    view = ChatView()
    view.set_agents(AGENTS)
    view.handle_event(TurnStarted("영희", 1))
    view.handle_event(TextDelta("영희", "말하다가"))
    view.handle_event(SystemNotice(Message(kind="system", text="오류로 일시정지")))
    view.handle_event(StateChanged(ConversationState.PAUSED))
    kinds = [type(w) for w in view.widgets()]
    assert kinds == [SystemLine]


def test_chat_view_message_kinds(qloop):
    view = ChatView()
    view.set_agents(AGENTS)
    view.add_message(Message(kind="agent", speaker="영희", text="**굵게**"))
    view.add_message(Message(kind="user", text="사용자 개입"))
    view.add_message(Message(kind="system", text="알림"))
    assert [type(w) for w in view.widgets()] == [AgentBubble, UserBubble, SystemLine]


# ── 메인 창 + 엔진 ─────────────────────────────────
def test_main_window_runs_conversation_with_fake_agents(qloop, tmp_path, monkeypatch):
    monkeypatch.setattr(
        main_window,
        "create_agent_runners",
        lambda config, *a, **kw: [FakeAgent(n) for n in config.agent_names],
    )
    win = MainWindow(tmp_path)
    config = SessionConfig(topic="테스트 대화", agents=AGENTS, max_turns=3)
    qloop.run_until_complete(win.start_conversation(config, autostart=False))
    assert win.start_btn.text() == "▶ 시작"
    win._run()
    qloop.run_until_complete(win._task)

    bubbles = [w for w in win.chat.widgets() if isinstance(w, AgentBubble)]
    assert [b.agent.name for b in bubbles] == ["철수", "영희", "철수"]
    assert all(not b.streaming for b in bubbles)
    assert win.orch.state == ConversationState.PAUSED
    assert "턴 3/3" in win.turn_label.text()
    assert not win.start_btn.isEnabled()  # 최대 턴 → 턴 추가로만 이어간다
    assert not win.add_turns_btn.isHidden()
    assert "토큰" in win.status_label.text()

    win._on_add_turns()
    qloop.run_until_complete(win._task)
    assert win.orch.turn == 13  # 턴 +10 후 새 최대치까지 진행
    qloop.run_until_complete(win.shutdown())
    assert all(a.disconnected for a in win.orch.agents)


def test_avatar_uses_image_or_initial(qloop, tmp_path):
    from PySide6.QtGui import QColor, QPixmap

    from app.gui.theme import avatar_pixmap

    img = tmp_path / "a.png"
    src = QPixmap(80, 40)
    src.fill(QColor("#ff0000"))
    src.save(str(img))
    with_image = avatar_pixmap("철수", "#0000ff", str(img), size=36).toImage()
    initial = avatar_pixmap("철수", "#0000ff", None, size=36).toImage()
    broken = avatar_pixmap("철수", "#0000ff", str(tmp_path / "없음.png"), size=36).toImage()
    assert with_image.size().width() == 36
    assert with_image.pixelColor(18, 6).red() > 200  # 이미지 색 (빨강)
    assert initial.pixelColor(18, 6).blue() > 200  # 지정 색 원 (파랑)
    assert broken.pixelColor(18, 6).blue() > 200  # 이미지가 없으면 이니셜로 대체
    assert initial.pixelColor(0, 0).alpha() == 0  # 원 바깥은 투명
