"""M8: 활동 카드 (도구 사용 요약)."""

import os

import pytest
from PySide6.QtWidgets import QApplication

from app.core.activity import describe_input, detail_lines, make_tool_use, summarize
from app.core.events import ToolActivity, TurnCompleted, TurnStarted
from app.core.models import AgentConfig, Message, SessionConfig
from app.core.orchestrator import Orchestrator
from app.core.sandbox import Decision
from tests.fakes import FakeAgent

RT = "mcp__roundtable__"


def test_describe_input_picks_the_key_argument():
    assert describe_input("Write", {"file_path": "C:\\a\\b\\메모.md", "content": "x"}) == "메모.md"
    assert describe_input("WebSearch", {"query": "남산타워 높이"}) == "남산타워 높이"
    assert describe_input("Grep", {"pattern": "비밀", "path": "C:\\s\\영희"}) == "비밀 (in 영희)"
    assert describe_input(f"{RT}respond_access_request", {"request_id": 3, "grant": True}) == (
        "#3 허가"
    )
    assert describe_input(f"{RT}vote_end", {"agree": False}) == "반대"
    assert describe_input("Read", {}) == ""
    assert len(describe_input("WebFetch", {"url": "https://x.com/" + "a" * 200})) == 80


def test_summary_and_details():
    uses = [
        make_tool_use("WebSearch", {"query": "q"}, False, ""),
        make_tool_use("Write", {"file_path": "a.md"}, False, ""),
        make_tool_use("Edit", {"file_path": "a.md"}, False, ""),  # 같은 파일은 한 번
        make_tool_use("Write", {"file_path": "b.md"}, False, ""),
        make_tool_use(f"{RT}request_folder_access", {"target": "영희"}, False, ""),
        make_tool_use("Read", {"file_path": "../영희/s.md"}, True, "권한이 없습니다."),
        make_tool_use("Bash", {"command": "dir"}, True, "사용할 수 없습니다."),
    ]
    assert summarize(uses) == "파일 2개 수정 · 웹 검색 1회 · 열람 요청·응답 1건 · 거부 2건"
    lines = detail_lines(uses)
    assert lines[0] == "· 웹 검색: q"
    assert lines[4] == "· 열람 요청: 영희"
    assert lines[5] == "✗ 파일 읽기: s.md — 권한이 없습니다."
    assert lines[6] == "✗ Bash — 사용할 수 없습니다."  # 모르는 도구는 원래 이름, 대상 없음
    assert summarize([]) == ""


class ToolUsingAgent(FakeAgent):
    async def run_turn(self, prompt, on_text=None, on_tool=None):
        if on_tool:
            on_tool("WebSearch", {"query": "날씨"}, Decision(True))
            on_tool("Read", {"file_path": "C:\\Windows\\win.ini"}, Decision(False, "권한 없음"))
        return await super().run_turn(prompt, on_text, on_tool)


async def test_orchestrator_attaches_activities_to_reply():
    config = SessionConfig(
        topic="t", agents=[AgentConfig(name="철수"), AgentConfig(name="영희")], max_turns=2
    )
    events: list = []
    orch = Orchestrator(config, [ToolUsingAgent("철수"), FakeAgent("영희")], events.append)
    await orch.run()
    first, second = [m for m in orch.transcript if m.kind == "agent"]
    assert [(u.tool, u.target, u.denied) for u in first.activities] == [
        ("WebSearch", "날씨", False),
        ("Read", "win.ini", True),
    ]
    assert second.activities == []
    assert len([e for e in events if isinstance(e, ToolActivity)]) == 2
    # transcript에 저장했다가 다시 읽어도 남는다
    restored = Message.model_validate_json(first.model_dump_json())
    assert restored.activities == first.activities


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_activity_card_fills_live_and_on_restore(app):
    from app.gui.bubble import AgentBubble
    from app.gui.chat_view import ChatView

    agents = [AgentConfig(name="철수"), AgentConfig(name="영희")]
    view = ChatView()
    view.set_agents(agents)
    view.handle_event(TurnStarted("철수", 1))
    bubble = view.widgets()[0]
    assert bubble.card.isHidden()  # 도구를 안 썼으면 카드도 없다
    view.handle_event(ToolActivity("철수", "WebSearch", {"query": "날씨"}, False))
    assert not bubble.card.isHidden() and bubble.card.summary == "웹 검색 1회"
    assert bubble.card.details.isHidden()
    bubble.card.toggle.setChecked(True)  # 펼치기
    assert not bubble.card.details.isHidden()
    assert bubble.card.toggle.text().startswith("▾")

    final = Message(
        kind="agent",
        speaker="철수",
        text="맑대요",
        turn=1,
        activities=[make_tool_use("WebSearch", {"query": "날씨"}, False, "")],
    )
    view.handle_event(TurnCompleted(final))
    assert bubble.card.summary == "웹 검색 1회"

    # 불러온 대화에서도 같은 카드가 나온다
    other = ChatView()
    other.set_agents(agents)
    other.add_message(final)
    restored = other.widgets()[0]
    assert isinstance(restored, AgentBubble) and restored.card.summary == "웹 검색 1회"
    other.add_message(Message(kind="agent", speaker="영희", text="네", turn=2))
    assert other.widgets()[1].card.isHidden()
