"""샌드박스 우회 시도 차단 테스트. 규칙을 고치면 여기에 케이스를 추가한다."""

import os
from pathlib import Path

import pytest

from app.core.sandbox import Sandbox


@pytest.fixture
def env(tmp_path: Path):
    session = tmp_path / "session"
    agents = session / "agents"
    me = agents / "철수"
    other = agents / "영희"
    lookalike = agents / "철수2"
    for d in (me, other, lookalike):
        d.mkdir(parents=True)
    (me / "notes.md").write_text("내 메모", encoding="utf-8")
    (other / "secret.md").write_text("영희 비밀", encoding="utf-8")
    (session / "session.json").write_text("{}", encoding="utf-8")
    (session / "acl.json").write_text("{}", encoding="utf-8")
    (session / "transcript.jsonl").write_text("", encoding="utf-8")
    granted: list[Path] = []
    sb = Sandbox(me, readable_dirs=lambda: granted, protected_root=session)
    return sb, me, other, lookalike, session, granted


def ok(sb: Sandbox, tool: str, **inp) -> bool:
    return sb.check(tool, inp).allowed


# ── 허용되어야 하는 것 ─────────────────────────────
def test_own_folder_read_write(env):
    sb, me, *_ = env
    assert ok(sb, "Read", file_path="notes.md")
    assert ok(sb, "Read", file_path=str(me / "notes.md"))
    assert ok(sb, "Write", file_path="new/검색결과.md")
    assert ok(sb, "Write", file_path=str(me / "a.txt"))
    assert ok(sb, "Edit", file_path="notes.md")
    assert ok(sb, "Glob", pattern="**/*.md")
    assert ok(sb, "Glob", pattern="*.md", path=str(me))
    assert ok(sb, "Grep", pattern="메모")
    assert ok(sb, "Grep", pattern="x", path="notes.md", glob="*.md")
    assert ok(sb, "Read", file_path="sub/../notes.md")  # 정규화 후 내부면 허용


def test_web_and_custom_tools_allowed(env):
    sb, *_ = env
    assert ok(sb, "WebSearch", query="날씨")
    assert ok(sb, "WebFetch", url="https://example.com", prompt="요약")
    assert ok(sb, "mcp__roundtable__request_folder_access", target="영희")


def test_case_insensitive_own_path_on_windows(env):
    sb, me, *_ = env
    if os.name != "nt":
        pytest.skip("Windows 전용")
    assert ok(sb, "Read", file_path=str(me / "NOTES.MD").upper())


# ── 차단되어야 하는 것 ─────────────────────────────
def test_unknown_and_dangerous_tools_denied(env):
    sb, *_ = env
    for tool in ("Bash", "NotebookEdit", "Task", "Agent", "MultiEdit", "KillShell", "Skill"):
        assert not ok(sb, tool, command="dir"), tool
    # 다른 MCP 서버(예: 사용자 계정 커넥터) 도구
    assert not ok(sb, "mcp__claude_ai_Gmail__send_message", to="x")
    assert not ok(sb, "mcp__roundtable_evil__x")


def test_parent_traversal_denied(env):
    sb, *_ = env
    assert not ok(sb, "Read", file_path="../영희/secret.md")
    assert not ok(sb, "Read", file_path="..\\영희\\secret.md")
    assert not ok(sb, "Write", file_path="../../session.json")
    assert not ok(sb, "Read", file_path="sub/../../영희/secret.md")
    assert not ok(sb, "Write", file_path="../철수2/x.txt")


def test_absolute_paths_outside_denied(env):
    sb, _, other, _, session, _ = env
    assert not ok(sb, "Read", file_path=str(other / "secret.md"))
    assert not ok(sb, "Read", file_path=str(session / "session.json"))
    assert not ok(sb, "Read", file_path="C:\\Windows\\win.ini")
    assert not ok(sb, "Read", file_path="/etc/passwd")
    assert not ok(sb, "Write", file_path=str(Path.home() / "evil.txt"))


def test_prefix_lookalike_folder_denied(env):
    sb, _, _, lookalike, *_ = env
    # 'agents/철수2'는 'agents/철수'로 시작하지만 다른 폴더다
    assert not ok(sb, "Read", file_path=str(lookalike / "x"))
    assert not ok(sb, "Write", file_path=str(lookalike / "x"))


def test_home_and_unc_and_device_paths_denied(env):
    sb, *_ = env
    assert not ok(sb, "Read", file_path="~/secret.txt")
    assert not ok(sb, "Read", file_path="~")
    assert not ok(sb, "Read", file_path="\\\\server\\share\\x.txt")
    assert not ok(sb, "Read", file_path="\\\\?\\C:\\Windows\\win.ini")
    assert not ok(sb, "Read", file_path="\\\\.\\C:\\Windows\\win.ini")


def test_missing_or_bad_arguments_denied(env):
    sb, *_ = env
    assert not ok(sb, "Read")
    assert not ok(sb, "Read", file_path="")
    assert not ok(sb, "Read", file_path=123)
    assert not ok(sb, "Write", file_path=["a"])
    assert not ok(sb, "Glob", pattern="*", path=5)


def test_glob_grep_escape_denied(env):
    sb, _, other, _, session, _ = env
    assert not ok(sb, "Glob", pattern="../**/*")
    assert not ok(sb, "Glob", pattern="**/../../**")
    assert not ok(sb, "Glob", pattern="{..,x}/**")
    assert not ok(sb, "Glob", pattern="C:/**/*")
    assert not ok(sb, "Glob", pattern="/**/*")
    assert not ok(sb, "Glob", pattern="\\**")
    assert not ok(sb, "Glob", pattern="~/**")
    assert not ok(sb, "Glob", pattern="*", path="..")
    assert not ok(sb, "Glob", pattern="*", path=str(session))
    assert not ok(sb, "Grep", pattern="비밀", path=str(other))
    assert not ok(sb, "Grep", pattern="x", path="..")
    assert not ok(sb, "Grep", pattern="x", glob="../**")


def test_protected_files_denied_even_if_granted(env):
    sb, _, _, _, session, granted = env
    granted.append(session)  # 잘못된 ACL로 세션 폴더 전체가 허가되어도
    assert not ok(sb, "Read", file_path=str(session / "session.json"))
    assert not ok(sb, "Read", file_path=str(session / "ACL.JSON"))
    assert not ok(sb, "Read", file_path=str(session / "transcript.jsonl"))


def test_symlink_escape_denied(env, tmp_path):
    sb, me, other, *_ = env
    link = me / "link_to_other"
    try:
        os.symlink(other, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음 (Windows 개발자 모드 필요)")
    assert not ok(sb, "Read", file_path="link_to_other/secret.md")
    assert not ok(sb, "Write", file_path="link_to_other/x.txt")
    assert not ok(sb, "Grep", pattern="x", path="link_to_other")


def test_junction_escape_denied(env):
    sb, me, other, *_ = env
    if os.name != "nt":
        pytest.skip("Windows 전용")
    import subprocess

    link = me / "junction"
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(other)], capture_output=True, check=False
    )
    if r.returncode != 0:
        pytest.skip("junction 생성 실패")
    assert not ok(sb, "Read", file_path="junction/secret.md")
    assert not ok(sb, "Write", file_path="junction/x.txt")


# ── ACL 허가 ──────────────────────────────────────
def test_granted_folder_is_read_only(env):
    sb, _, other, _, _, granted = env
    assert not ok(sb, "Read", file_path=str(other / "secret.md"))
    granted.append(other)
    assert ok(sb, "Read", file_path=str(other / "secret.md"))
    assert ok(sb, "Read", file_path="../영희/secret.md")
    assert ok(sb, "Grep", pattern="비밀", path=str(other))
    assert not ok(sb, "Write", file_path=str(other / "x.txt"))
    assert not ok(sb, "Edit", file_path=str(other / "secret.md"))
    granted.clear()  # 철회 즉시 반영
    assert not ok(sb, "Read", file_path=str(other / "secret.md"))
