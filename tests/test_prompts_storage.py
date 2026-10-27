from datetime import datetime

from app.core.models import AgentConfig, SessionConfig
from app.core.prompts import build_system_prompt
from app.core.storage import agent_dir, new_session_dir, safe_dirname


def test_system_prompt_includes_only_own_instructions():
    config = SessionConfig(
        topic="고양이 vs 강아지 토론",
        agents=[
            AgentConfig(name="철수", instructions="고양이 편"),
            AgentConfig(name="영희", instructions="강아지 편"),
        ],
    )
    p = build_system_prompt(config, config.agents[0])
    assert "당신은 '철수'" in p
    assert "다른 참가자: 영희" in p
    assert "고양이 vs 강아지 토론" in p
    assert "고양이 편" in p
    assert "강아지 편" not in p


def test_safe_dirname():
    assert safe_dirname("철수") == "철수"
    assert safe_dirname('a<b>:c"/d\\e|f?g*') == "a_b__c__d_e_f_g"
    assert safe_dirname("  ..  ") == "untitled"
    assert safe_dirname("CON") == "_CON"
    assert safe_dirname("../../etc") == "etc"
    assert len(safe_dirname("가" * 100)) == 40


def test_new_session_dir(tmp_path):
    now = datetime(2026, 10, 27, 15, 30, 0)
    d1 = new_session_dir(tmp_path, "토론 주제\n세부 규칙", now)
    d2 = new_session_dir(tmp_path, "토론 주제", now)
    assert d1.name == "2026-10-27_153000_토론_주제"
    assert d2.name.endswith("_2")
    assert (d1 / "agents").is_dir()
    assert agent_dir(d1, "철수") == d1 / "agents" / "철수"
