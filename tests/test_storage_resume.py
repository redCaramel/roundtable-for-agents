"""M7: 자동 저장과 이어하기."""

import json

import pytest

from app.core import session_factory
from app.core.models import AgentConfig, ConversationState, EndPolicy, SessionConfig
from app.core.prompts import RESTORE_MAX_CHARS, build_restore_context
from app.core.session_factory import build_orchestrator, load_orchestrator
from app.core.storage import SessionStore, list_sessions, new_session_dir
from tests.fakes import FakeAgent

NAMES = ["철수", "영희", "민수"]


@pytest.fixture
def fake_runners(monkeypatch):
    """build_orchestrator가 만드는 러너를 가짜로 바꾸고, 넘겨받은 이어하기 인자를 기록한다."""
    created: list[dict] = []

    def make(config, session_dir, **kw):
        agents = [FakeAgent(n) for n in config.agent_names]
        resume = kw.get("resume_sessions") or {}
        for a in agents:
            a.session_id = resume.get(a.name) or f"sess-{a.name}"
        created.append({"agents": agents, "resume": resume, "transcript": kw.get("transcript")})
        return agents

    monkeypatch.setattr(session_factory, "create_agent_runners", make)
    return created


def _config(**kw):
    return SessionConfig(
        topic="이어하기 시험\n둘째 줄", agents=[AgentConfig(name=n) for n in NAMES], **kw
    )


def _speakers(orch):
    return [m.speaker for m in orch.transcript if m.kind == "agent"]


async def test_autosave_and_resume_continues_seamlessly(tmp_path, fake_runners):
    d = new_session_dir(tmp_path, "이어하기 시험")
    orch = build_orchestrator(_config(max_turns=2), d)
    await orch.send_user_message("@영희 먼저")
    await orch.run()
    await orch.close()

    # 파일이 남는다
    assert (d / "session.json").is_file() and (d / "transcript.jsonl").is_file()
    lines = (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["kind"] for x in lines] == ["user", "agent", "agent", "system"]
    snap = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert snap["state"] == "paused" and snap["turn"] == 2
    assert snap["agent_sessions"]["영희"] == "sess-영희"

    # 목록에 보인다
    [info] = list_sessions(tmp_path)
    assert info.topic == "이어하기 시험" and info.turn == 2 and info.agent_names == NAMES

    # 프로그램을 다시 켠 것처럼 불러온다
    resumed = await load_orchestrator(d)
    assert fake_runners[-1]["resume"] == {n: f"sess-{n}" for n in NAMES}
    assert resumed.state == ConversationState.PAUSED
    assert [m.text for m in resumed.transcript] == [m.text for m in orch.transcript]
    assert resumed.usage == orch.usage
    a = {x.name: x for x in fake_runners[-1]["agents"]}

    resumed.add_turns(2)
    await resumed.run()
    await resumed.close()
    # 첫 실행: 영희(지목) → 다음 순서 민수. 이어한 뒤: 그다음 순서인 철수부터, 턴 번호도 이어진다
    assert _speakers(resumed) == ["영희", "민수", "철수", "영희"]
    assert [m.turn for m in resumed.transcript if m.kind == "agent"] == [1, 2, 3, 4]
    # 이미 본 메시지는 다시 보내지 않는다 (철수는 처음이라 전부, 영희는 자기 차례 이후만)
    assert "[사용자]: @영희 먼저" in a["철수"].prompts[0]
    assert "[사용자]: @영희 먼저" not in a["영희"].prompts[0]
    assert "민수: 민수의 1번째 발언" in a["영희"].prompts[0]
    assert "철수: 철수의 1번째 발언" in a["영희"].prompts[0]
    assert len((d / "transcript.jsonl").read_text(encoding="utf-8").splitlines()) == 7


async def test_ended_conversation_reopens_as_paused(tmp_path, fake_runners):
    d = new_session_dir(tmp_path, "t")
    orch = build_orchestrator(_config(max_turns=5, end_policy=EndPolicy.ANY), d)
    orch.end_vote.propose("철수", "끝")
    await orch.run()
    await orch.close()
    assert orch.state == ConversationState.ENDED

    resumed = await load_orchestrator(d)
    assert resumed.state == ConversationState.PAUSED
    assert resumed.end_vote.ended_reason is None  # 종료 결정을 지우고 이어간다
    await resumed.run()
    await resumed.close()
    assert resumed.turn == 5


async def test_unfinished_vote_and_acl_survive_restart(tmp_path, fake_runners):
    d = new_session_dir(tmp_path, "t")
    orch = build_orchestrator(_config(max_turns=1), d)
    orch.acl.request("철수", "영희", "자료", turn=0)
    orch.acl.respond("영희", 1, True, "")
    orch.end_vote.propose("민수", "그만")
    orch.end_vote.vote("철수", True, "")
    await orch.run()
    await orch.close()

    resumed = await load_orchestrator(d)
    assert resumed.acl.readable_owners("철수") == ["영희"]
    assert resumed.end_vote.pending_voters() == ["영희"]
    await resumed.close()


async def test_crash_while_running_restores_as_paused(tmp_path, fake_runners):
    d = new_session_dir(tmp_path, "t")
    orch = build_orchestrator(_config(max_turns=3), d)
    await orch.run()
    await orch.close()
    # 진행 중에 프로그램이 죽은 상황을 흉내 낸다
    snap = json.loads((d / "session.json").read_text(encoding="utf-8"))
    snap["state"] = "running"
    (d / "session.json").write_text(json.dumps(snap), encoding="utf-8")
    resumed = await load_orchestrator(d)
    assert resumed.state == ConversationState.PAUSED
    await resumed.close()


def test_load_tolerates_broken_and_extra_lines(tmp_path):
    d = new_session_dir(tmp_path, "t")
    store = SessionStore(d)
    from app.core.models import Message
    from app.core.storage import SessionSnapshot

    msgs = [Message(kind="user", text=f"m{i}") for i in range(3)]
    for m in msgs:
        store.append_message(m)
    store.save_snapshot(SessionSnapshot(config=_config(), max_turns=30, message_count=2))
    store.flush()
    with (d / "transcript.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"kind": "agent", "te')  # 쓰다 끊긴 줄
    store.close()

    snap, loaded = SessionStore(d).load()
    # 깨진 줄은 버리고, 스냅샷보다 앞서 기록된 메시지(m2)는 잘라 상태와 맞춘다
    assert [m.text for m in loaded] == ["m0", "m1"]


def test_list_sessions_skips_broken_and_sorts_recent_first(tmp_path):
    from datetime import datetime, timedelta

    from app.core.storage import SessionSnapshot

    for i, topic in enumerate(["오래된", "최근"]):
        d = new_session_dir(tmp_path, topic)
        store = SessionStore(d)
        snap = SessionSnapshot(
            config=SessionConfig(topic=topic, agents=[AgentConfig(name=n) for n in NAMES]),
            max_turns=30,
            updated_at=datetime(2026, 1, 1) + timedelta(days=i),
        )
        store.save_snapshot(snap)
        store.close()
    broken = tmp_path / "깨진폴더"
    broken.mkdir()
    (broken / "session.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "빈폴더").mkdir()
    assert [i.topic for i in list_sessions(tmp_path)] == ["최근", "오래된"]
    assert list_sessions(tmp_path / "없음") == []


def test_restore_context_keeps_recent_messages_within_budget():
    from app.core.models import Message

    assert build_restore_context("철수", []) == ""
    msgs = [Message(kind="agent", speaker="영희", text=f"{i}번 " + "가" * 1000) for i in range(40)]
    ctx = build_restore_context("철수", msgs)
    assert ctx.startswith("[이전 대화 복원]") and ctx.endswith("[복원 끝]")
    assert "영희: 39번" in ctx and "영희: 0번" not in ctx and "생략" in ctx
    assert len(ctx) < RESTORE_MAX_CHARS + 500


async def test_runner_falls_back_to_new_session_with_context(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    from app.core import agent_runner as ar

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    clients: list = []

    class FakeClient:
        def __init__(self, options):
            self.options = options
            self.prompts: list[str] = []
            clients.append(self)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def query(self, prompt):
            self.prompts.append(prompt)

        async def receive_response(self):
            if self.options.resume:
                raise RuntimeError("No conversation found with session ID")
            yield AssistantMessage(content=[TextBlock("이어서 말합니다")], model="m")
            yield ResultMessage("success", 1, 1, False, 1, "new-sess", result="이어서 말합니다")

    monkeypatch.setattr(ar, "ClaudeSDKClient", FakeClient)
    runner = ar.AgentRunner(
        "철수",
        "haiku",
        "",
        tmp_path,
        resume_session_id="old-sess",
        restore_context=lambda: "[이전 대화 복원] 영희: 안녕",
    )
    result = await runner.run_turn("이제 당신 차례")
    assert result.text == "이어서 말합니다" and runner.session_id == "new-sess"
    assert clients[0].options.resume == "old-sess"
    assert clients[1].options.resume is None
    assert clients[1].prompts == ["[이전 대화 복원] 영희: 안녕\n\n이제 당신 차례"]

    # 한 번 성공한 뒤의 오류는 그대로 올려보낸다 (다시 복원하지 않는다)
    async def boom(prompt, on_text):
        raise RuntimeError("네트워크 오류")

    monkeypatch.setattr(runner, "_run_turn", boom)
    with pytest.raises(RuntimeError, match="네트워크"):
        await runner.run_turn("다음")
