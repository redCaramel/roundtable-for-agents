from pathlib import Path

import pytest

from app.core.access_control import AccessControl, AccessError, RequestStatus
from app.core.sandbox import Sandbox
from app.core.session_factory import acl_readable_dirs
from app.core.storage import agent_dir
from app.core.tools import build_access_tools

NAMES = ["철수", "영희", "민수"]


@pytest.fixture
def acl():
    a = AccessControl(NAMES)
    a.notices = []  # type: ignore[attr-defined]
    a.subscribe(a.notices.append)  # type: ignore[attr-defined]
    return a


def test_full_flow_request_grant_revoke(acl):
    req = acl.request("철수", "영희", "자료 확인", turn=1)
    assert req.id == 1 and req.status == RequestStatus.PENDING
    assert acl.pending_for("영희") == [req]
    assert acl.readable_owners("철수") == []

    acl.respond("영희", 1, grant=True, reason="참고하세요")
    assert acl.get(1).status == RequestStatus.GRANTED
    assert acl.readable_owners("철수") == ["영희"]
    assert acl.readers_of("영희") == ["철수"]
    assert acl.readable_owners("영희") == []  # 허가는 한 방향

    acl.revoke("영희", "철수")
    assert acl.readable_owners("철수") == []
    assert len(acl.notices) == 3
    assert "요청 #1" in acl.notices[0] and "허가" in acl.notices[1] and "철회" in acl.notices[2]


def test_deny(acl):
    acl.request("철수", "영희", "궁금", turn=0)
    acl.respond("영희", 1, grant=False, reason="비공개")
    assert acl.get(1).status == RequestStatus.DENIED
    assert acl.readable_owners("철수") == []
    assert "거부" in acl.notices[-1] and "비공개" in acl.notices[-1]
    # 거부된 뒤에는 다시 요청할 수 있다
    assert acl.request("철수", "영희", "다시", turn=1).id == 2


@pytest.mark.parametrize(
    "requester,target",
    [("철수", "철수"), ("철수", "없는사람"), ("없는사람", "영희")],
)
def test_invalid_requests(acl, requester, target):
    with pytest.raises(AccessError):
        acl.request(requester, target, "x", turn=0)


def test_duplicate_and_already_granted(acl):
    acl.request("철수", "영희", "x", turn=0)
    with pytest.raises(AccessError):
        acl.request("철수", "영희", "또", turn=0)
    acl.respond("영희", 1, True, "")
    with pytest.raises(AccessError):
        acl.request("철수", "영희", "또", turn=0)


def test_only_target_can_respond_once(acl):
    acl.request("철수", "영희", "x", turn=0)
    with pytest.raises(AccessError):
        acl.respond("민수", 1, True, "")  # 제3자가 대신 허가할 수 없다
    with pytest.raises(AccessError):
        acl.respond("철수", 1, True, "")  # 요청자 자신도 불가
    with pytest.raises(AccessError):
        acl.respond("영희", 99, True, "")
    acl.respond("영희", 1, False, "")
    with pytest.raises(AccessError):
        acl.respond("영희", 1, True, "")  # 이미 처리됨
    assert acl.readable_owners("철수") == []


def test_revoke_only_own_grants(acl):
    acl.request("철수", "영희", "x", turn=0)
    acl.respond("영희", 1, True, "")
    with pytest.raises(AccessError):
        acl.revoke("민수", "철수")  # 민수는 철수에게 준 허가가 없다
    with pytest.raises(AccessError):
        acl.revoke("영희", "민수")
    assert acl.readable_owners("철수") == ["영희"]


def test_expire(acl):
    acl.request("철수", "영희", "x", turn=2)
    assert acl.expire(11) == []
    assert [r.id for r in acl.expire(12)] == [1]
    assert acl.get(1).status == RequestStatus.EXPIRED
    assert "만료" in acl.notices[-1]
    with pytest.raises(AccessError):
        acl.respond("영희", 1, True, "")


def test_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "acl.json"
    a = AccessControl(NAMES, path)
    a.request("철수", "영희", "x", turn=0)
    a.respond("영희", 1, True, "")
    a.request("민수", "철수", "y", turn=1)
    b = AccessControl.load(NAMES, path)
    assert b.readable_owners("철수") == ["영희"]
    assert [r.id for r in b.pending_for("철수")] == [2]
    assert b.request("영희", "민수", "z", turn=2).id == 3


def test_acl_drives_sandbox_reads(tmp_path: Path):
    acl = AccessControl(NAMES)
    for n in NAMES:
        agent_dir(tmp_path, n).mkdir(parents=True)
    secret = agent_dir(tmp_path, "영희") / "s.md"
    secret.write_text("x", encoding="utf-8")
    dirs = acl_readable_dirs(acl, tmp_path)
    sb = Sandbox(agent_dir(tmp_path, "철수"), lambda: dirs("철수"), protected_root=tmp_path)

    assert not sb.check("Read", {"file_path": str(secret)}).allowed
    acl.request("철수", "영희", "x", turn=0)
    assert not sb.check("Read", {"file_path": str(secret)}).allowed  # 요청만으로는 불가
    acl.respond("영희", 1, True, "")
    assert sb.check("Read", {"file_path": str(secret)}).allowed
    assert not sb.check("Write", {"file_path": str(secret)}).allowed  # 읽기 전용
    acl.revoke("영희", "철수")
    assert not sb.check("Read", {"file_path": str(secret)}).allowed


# ── 도구 핸들러 ────────────────────────────────────
def _tools(acl: AccessControl, name: str):
    specs = build_access_tools(acl, name, lambda: 3, folder_of=lambda o: f"/x/{o}")
    return {s.name: s.handler for s in specs}


def test_tool_handlers(acl):
    cs, yh = _tools(acl, "철수"), _tools(acl, "영희")
    out = cs["request_folder_access"]({"target": "영희", "reason": "자료"})
    assert not out.is_error and "#1" in out.text
    assert acl.get(1).created_turn == 3

    listing = yh["list_my_access"]({}).text
    assert "#1 철수" in listing

    assert not yh["respond_access_request"]({"request_id": 1, "grant": True}).is_error
    assert "/x/영희" in cs["list_my_access"]({}).text
    assert not yh["revoke_access"]({"agent": "철수"}).is_error


def test_tool_handlers_report_errors_instead_of_raising(acl):
    cs = _tools(acl, "철수")
    assert cs["request_folder_access"]({"target": "철수", "reason": ""}).is_error
    assert cs["request_folder_access"]({}).is_error  # 필수값 누락
    assert cs["respond_access_request"]({"request_id": 1, "grant": True}).is_error
    assert cs["respond_access_request"]({"request_id": "x", "grant": True}).is_error
    acl.request("영희", "철수", "x", turn=0)
    assert cs["respond_access_request"]({"request_id": 1, "grant": "yes"}).is_error
    assert acl.readable_owners("영희") == []
    assert cs["revoke_access"]({"agent": "영희"}).is_error
