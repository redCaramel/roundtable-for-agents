"""GUI 위젯 테스트 (offscreen). 실제 에이전트 대신 가짜 에이전트를 쓴다."""

import asyncio

import pytest
import qasync
from PySide6.QtWidgets import QApplication

from app.core import session_factory
from app.core.events import StateChanged, SystemNotice, TextDelta, TurnCompleted, TurnStarted
from app.core.models import AgentConfig, ConversationState, Message, SessionConfig
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
        session_factory,
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


# ── M5: 사용자 제어 ────────────────────────────────
def test_message_input_enter_sends_shift_enter_newlines(qloop):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest

    from app.gui.message_input import MessageInput

    box = MessageInput()
    sent: list[str] = []
    box.submitted.connect(sent.append)
    # QTest.keyClicks는 한글을 키 입력으로 바꾸지 못해 죽으므로 글자는 직접 넣는다
    box.setPlainText("첫 줄")
    box.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(box, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    box.insertPlainText("둘째 줄")
    assert sent == []
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert sent == ["첫 줄\n둘째 줄"]
    assert box.toPlainText() == ""
    QTest.keyClick(box, Qt.Key.Key_Return)  # 빈 입력은 보내지 않는다
    assert sent == ["첫 줄\n둘째 줄"]


def _slow_window(tmp_path, monkeypatch):
    """철수는 오래 말하고(중단 대상) 영희는 짧게 말하는 창. 시나리오는 실제 앱처럼
    run_until_complete 하나 안에서 돌린다 (여러 번 끊어 돌리면 qasync가 루프 밖에서
    태스크를 진행시켜 asyncio.sleep이 실패한다)."""
    from tests.fakes import SlowAgent

    agents = {"철수": SlowAgent("철수", chunks=400), "영희": SlowAgent("영희", chunks=2)}
    monkeypatch.setattr(
        session_factory,
        "create_agent_runners",
        lambda config, *a, **kw: [agents[n] for n in config.agent_names],
    )
    return MainWindow(tmp_path), agents


def test_main_window_user_intervention(qloop, tmp_path, monkeypatch):
    win, agents = _slow_window(tmp_path, monkeypatch)

    async def scenario():
        await win.start_conversation(SessionConfig(topic="제어", agents=AGENTS, max_turns=2))
        await agents["철수"].started.wait()
        assert win.pause_btn.isEnabled() and not win.start_btn.isEnabled()
        agents["철수"].chunks = 2
        win.input.setPlainText("@영희 먼저 말해 줘")
        win.input.submit()
        await win._task
        await win.shutdown()

    qloop.run_until_complete(scenario())
    kinds = [type(w).__name__ for w in win.chat.widgets()]
    # 중단 표시 → 사용자 말풍선 → 영희(지목) → 철수 → 최대 턴 알림
    assert kinds == ["SystemLine", "UserBubble", "AgentBubble", "AgentBubble", "SystemLine"]
    names = [w.agent.name for w in win.chat.widgets() if isinstance(w, AgentBubble)]
    assert names == ["영희", "철수"]


def test_main_window_pause_resume_and_stop(qloop, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel

    win, agents = _slow_window(tmp_path, monkeypatch)
    seen: dict = {}

    async def scenario():
        await win.start_conversation(SessionConfig(topic="제어", agents=AGENTS, max_turns=5))
        await agents["철수"].started.wait()
        win._on_pause()
        await win._control_future  # 일시정지 처리가 끝나 버튼이 풀릴 때까지
        await win._task
        seen["paused"] = win.orch.state
        seen["start_enabled"] = win.start_btn.isEnabled()
        seen["pause_enabled"] = win.pause_btn.isEnabled()
        seen["last_line"] = win.chat.widgets()[-1].findChild(QLabel).text()

        win._on_start()  # 재개 → 중단됐던 철수부터
        await agents["철수"].started.wait()
        seen["speaking"] = win.orch.speaking
        await win.stop_now()
        await win._task
        await win.shutdown()

    qloop.run_until_complete(scenario())
    assert seen["paused"] == ConversationState.PAUSED
    assert seen["start_enabled"] and not seen["pause_enabled"]
    assert "일시정지로 중단" in seen["last_line"]
    assert seen["speaking"] == "철수"
    assert win.orch.state == ConversationState.ENDED
    assert not win.input.isEnabled() and not win.start_btn.isEnabled()
    assert not win.stop_btn.isEnabled()
    assert "종료됨" in win.turn_label.text()


# ── M6: 진행자 / 종료 정책 ─────────────────────────
def test_setup_dialog_moderator_options(qloop):
    from app.core.models import EndPolicy, TurnMode

    d = SetupDialog()
    d.topic.setPlainText("t")
    declare = d.end_policy.findData(EndPolicy.MODERATOR)
    assert not d.moderator_model.isEnabled()
    assert not d.end_policy.model().item(declare).isEnabled()

    d.turn_mode.setCurrentIndex(d.turn_mode.findData(TurnMode.MODERATOR))
    assert d.moderator_model.isEnabled()
    assert d.end_policy.model().item(declare).isEnabled()
    d.end_policy.setCurrentIndex(declare)
    d.moderator_model.setCurrentIndex(d.moderator_model.findData("sonnet"))
    config = d.build_config()
    assert config.turn_mode == TurnMode.MODERATOR
    assert config.end_policy == EndPolicy.MODERATOR
    assert config.moderator_model == "sonnet"

    # 라운드로빈으로 돌리면 '진행자 선언'은 고를 수 없게 되고 기본값으로 돌아간다
    d.turn_mode.setCurrentIndex(d.turn_mode.findData(TurnMode.ROUND_ROBIN))
    assert d.end_policy.currentData() == EndPolicy.UNANIMOUS


def test_main_window_shows_moderator_decision(qloop, tmp_path, monkeypatch):
    from app.core.models import EndPolicy, TurnMode
    from tests.test_moderator_end import FakeModerator

    monkeypatch.setattr(
        session_factory,
        "create_agent_runners",
        lambda config, *a, **kw: [FakeAgent(n) for n in config.agent_names],
    )
    mod = FakeModerator([("영희", "철수에게 질문받음"), (None, "결론이 났다", True)])
    monkeypatch.setattr(session_factory, "create_moderator", lambda config, d: mod)
    win = MainWindow(tmp_path)
    config = SessionConfig(
        topic="t",
        agents=AGENTS,
        turn_mode=TurnMode.MODERATOR,
        end_policy=EndPolicy.MODERATOR,
        max_turns=5,
    )

    async def scenario():
        await win.start_conversation(config)
        await win._task
        await win.shutdown()

    qloop.run_until_complete(scenario())
    names = [w.agent.name for w in win.chat.widgets() if isinstance(w, AgentBubble)]
    assert names == ["영희"]
    assert win.orch.state == ConversationState.ENDED
    assert win.moderator_label.text() == "진행자 → 종료"
    assert win.moderator_label.toolTip() == "결론이 났다"
    assert "종료됨" in win.turn_label.text()
