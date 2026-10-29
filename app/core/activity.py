"""활동 카드 내용 (plan.md 6.3): 한 턴의 도구 사용을 한 줄 요약과 상세 목록으로 만든다."""

from __future__ import annotations

from pathlib import PurePath
from typing import Any

from pydantic import BaseModel

from app.core.sandbox import CUSTOM_TOOL_PREFIX

# (분류, 요약 문구) — 이 순서대로 요약에 나온다.
_CATEGORIES: list[tuple[str, str]] = [
    ("write", "파일 {n}개 수정"),
    ("read", "파일 {n}개 읽음"),
    ("search", "파일 검색 {n}회"),
    ("web_search", "웹 검색 {n}회"),
    ("web_fetch", "웹 페이지 {n}개"),
    ("access", "열람 요청·응답 {n}건"),
    ("end", "종료 제안·투표 {n}건"),
    ("other", "기타 도구 {n}회"),
]
_LABELS = {
    "Write": "파일 쓰기",
    "Edit": "파일 수정",
    "Read": "파일 읽기",
    "Glob": "파일 찾기",
    "Grep": "내용 검색",
    "WebSearch": "웹 검색",
    "WebFetch": "웹 페이지",
    "request_folder_access": "열람 요청",
    "respond_access_request": "열람 응답",
    "revoke_access": "권한 철회",
    "list_my_access": "권한 확인",
    "propose_end": "종료 제안",
    "vote_end": "종료 투표",
}
_MAX_TARGET = 80


class ToolUse(BaseModel):
    """도구 호출 1건의 기록 (transcript에 저장되므로 입력 전체가 아닌 요약만 둔다)."""

    tool: str
    target: str = ""
    denied: bool = False
    reason: str = ""

    @property
    def short_name(self) -> str:
        return self.tool.removeprefix(CUSTOM_TOOL_PREFIX)


def _category(short: str) -> str:
    return {
        "Write": "write",
        "Edit": "write",
        "Read": "read",
        "Glob": "search",
        "Grep": "search",
        "WebSearch": "web_search",
        "WebFetch": "web_fetch",
        "request_folder_access": "access",
        "respond_access_request": "access",
        "revoke_access": "access",
        "list_my_access": "access",
        "propose_end": "end",
        "vote_end": "end",
    }.get(short, "other")


def describe_input(tool: str, tool_input: dict[str, Any]) -> str:
    """도구 입력에서 사람이 볼 핵심 한 가지(파일 이름, 검색어, 대상 등)를 뽑는다."""
    short = tool.removeprefix(CUSTOM_TOOL_PREFIX)
    value: Any = None
    if short in ("Write", "Edit", "Read"):
        raw = tool_input.get("file_path")
        value = PurePath(raw).name if isinstance(raw, str) and raw else raw
    elif short in ("Glob", "Grep"):
        value = tool_input.get("pattern")
        if tool_input.get("path"):
            value = f"{value} (in {PurePath(str(tool_input['path'])).name})"
    elif short == "WebSearch":
        value = tool_input.get("query")
    elif short == "WebFetch":
        value = tool_input.get("url")
    elif short == "request_folder_access":
        value = tool_input.get("target")
    elif short == "respond_access_request":
        verdict = "허가" if tool_input.get("grant") is True else "거부"
        value = f"#{tool_input.get('request_id')} {verdict}"
    elif short == "revoke_access":
        value = tool_input.get("agent")
    elif short == "vote_end":
        value = "찬성" if tool_input.get("agree") is True else "반대"
    elif short == "propose_end":
        value = tool_input.get("reason")
    text = "" if value is None else str(value)
    return text if len(text) <= _MAX_TARGET else text[: _MAX_TARGET - 1] + "…"


def make_tool_use(tool: str, tool_input: dict[str, Any], denied: bool, reason: str) -> ToolUse:
    return ToolUse(tool=tool, target=describe_input(tool, tool_input), denied=denied, reason=reason)


def summarize(uses: list[ToolUse]) -> str:
    """예: '파일 2개 수정 · 웹 검색 1회 · 거부 1건'. 사용이 없으면 빈 문자열."""
    if not uses:
        return ""
    allowed = [u for u in uses if not u.denied]
    parts: list[str] = []
    for cat, text in _CATEGORIES:
        hits = [u for u in allowed if _category(u.short_name) == cat]
        if not hits:
            continue
        if cat in ("write", "read", "web_fetch"):
            n = len({u.target for u in hits})  # 같은 파일·페이지는 한 번으로 센다
        else:
            n = len(hits)
        parts.append(text.format(n=n))
    denied = len(uses) - len(allowed)
    if denied:
        parts.append(f"거부 {denied}건")
    return " · ".join(parts)


def detail_lines(uses: list[ToolUse]) -> list[str]:
    """펼쳤을 때 보여줄 도구별 상세 (거부된 시도와 사유 포함)."""
    lines = []
    for u in uses:
        label = _LABELS.get(u.short_name, u.short_name)
        line = f"{'✗' if u.denied else '·'} {label}"
        if u.target:
            line += f": {u.target}"
        if u.denied and u.reason:
            line += f" — {u.reason}"
        lines.append(line)
    return lines
