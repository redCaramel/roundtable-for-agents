"""파일 샌드박스 (plan.md 5.2).

보안 경계이므로 수정 시 tests/test_sandbox.py에 우회 시도 케이스를 추가한다.

규칙
- 허용 목록 방식: 아래에 정의한 도구만 허용하고 나머지는 전부 거부한다.
- 쓰기(Write, Edit): 자기 폴더 내부만.
- 읽기(Read, Glob, Grep): 자기 폴더 + ACL로 허가받은 폴더만.
- 모든 경로는 에이전트 폴더 기준으로 해석한 뒤 Path.resolve()로 정규화해 판정한다
  (`..`, 심볼릭 링크, 절대경로, 대소문자 차이 우회 차단).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WRITE_TOOLS = frozenset({"Write", "Edit"})
READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
WEB_TOOLS = frozenset({"WebSearch", "WebFetch"})
FILE_TOOLS = WRITE_TOOLS | READ_TOOLS
SANDBOX_TOOLS: list[str] = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]

# 우리가 만든 인프로세스 MCP 서버 이름 (tools.py). 이 접두의 도구만 허용한다.
CUSTOM_MCP_SERVER = "roundtable"
CUSTOM_TOOL_PREFIX = f"mcp__{CUSTOM_MCP_SERVER}__"

# 대화 관리 파일 — 어떤 에이전트도 접근할 수 없다.
PROTECTED_FILES = frozenset({"session.json", "transcript.jsonl", "acl.json"})

DENY_HINT = (
    "권한이 없습니다. 당신은 자기 개인 폴더(현재 작업 폴더)에만 쓸 수 있고, "
    "다른 참가자의 폴더는 허가를 받아야 읽을 수 있습니다."
)

_DRIVE_OR_ROOT = re.compile(r"^([a-zA-Z]:|[\\/])")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True)


def _deny(reason: str) -> Decision:
    return Decision(False, f"{reason} {DENY_HINT}")


def _norm(p: Path) -> str:
    # Windows는 대소문자를 구분하지 않으므로 비교용으로 정규화한다.
    return os.path.normcase(str(p))


def is_within(target: Path, root: Path) -> bool:
    """target(이미 resolve됨)이 root(이미 resolve됨) 자신이거나 그 하위인지."""
    t, r = _norm(target), _norm(root)
    try:
        return os.path.commonpath([t, r]) == r
    except ValueError:  # 드라이브가 다르면 commonpath가 실패한다
        return False


class Sandbox:
    """에이전트 1명의 경로 검사기."""

    def __init__(
        self,
        own_dir: Path,
        readable_dirs: Callable[[], Iterable[Path]] = lambda: (),
        protected_root: Path | None = None,
    ) -> None:
        self.own_dir = own_dir.resolve()
        # ACL에서 현재 읽기 허가받은 폴더 목록 (호출할 때마다 최신 상태를 가져온다)
        self._readable_dirs = readable_dirs
        self.protected_root = protected_root.resolve() if protected_root else None

    # ── 경로 해석 ────────────────────────────────────
    def resolve(self, raw: str) -> Path:
        if raw.startswith("~"):
            raise ValueError("'~' 경로는 쓸 수 없습니다.")
        p = Path(raw)
        if not p.is_absolute():
            p = self.own_dir / p
        return p.resolve()

    def _is_protected(self, p: Path) -> bool:
        if p.name.lower() not in PROTECTED_FILES:
            return False
        return self.protected_root is None or is_within(p, self.protected_root)

    def _readable_roots(self) -> list[Path]:
        return [self.own_dir, *(Path(d).resolve() for d in self._readable_dirs())]

    def can_read(self, p: Path) -> bool:
        return not self._is_protected(p) and any(is_within(p, r) for r in self._readable_roots())

    def can_write(self, p: Path) -> bool:
        return not self._is_protected(p) and is_within(p, self.own_dir)

    # ── 판정 ─────────────────────────────────────────
    def check(self, tool_name: str, tool_input: dict[str, Any]) -> Decision:
        if tool_name in WEB_TOOLS:
            return ALLOW
        if tool_name.startswith(CUSTOM_TOOL_PREFIX):
            return ALLOW
        if tool_name not in FILE_TOOLS:
            return Decision(False, f"'{tool_name}' 도구는 이 대화에서 사용할 수 없습니다.")
        try:
            return self._check_file_tool(tool_name, tool_input)
        except (ValueError, OSError) as e:
            return _deny(f"경로를 해석할 수 없습니다 ({e}).")

    def _check_file_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Decision:
        if tool_name in ("Read", "Write", "Edit"):
            raw = tool_input.get("file_path")
            if not isinstance(raw, str) or not raw.strip():
                return _deny("file_path가 없습니다.")
            p = self.resolve(raw)
            ok = self.can_write(p) if tool_name in WRITE_TOOLS else self.can_read(p)
            return ALLOW if ok else _deny(f"'{raw}'에 접근할 수 없습니다.")

        # Glob / Grep: 기준 경로(path, 없으면 자기 폴더)와 패턴을 함께 본다.
        raw_path = tool_input.get("path")
        if raw_path is None or raw_path == "":
            base = self.own_dir
        elif isinstance(raw_path, str):
            base = self.resolve(raw_path)
        else:
            return _deny("path 형식이 잘못되었습니다.")
        if not self.can_read(base):
            return _deny(f"'{raw_path}'에 접근할 수 없습니다.")

        patterns = [tool_input.get("pattern")] if tool_name == "Glob" else []
        if tool_name == "Grep":
            patterns.append(tool_input.get("glob"))
        for pat in patterns:
            if pat is None:
                continue
            if not isinstance(pat, str) or not _is_safe_glob(pat):
                return _deny(f"패턴 '{pat}'은 허용되지 않습니다 (절대경로·'..'·'~' 금지).")
        return ALLOW


def _is_safe_glob(pattern: str) -> bool:
    """기준 폴더 밖을 가리킬 수 있는 glob 패턴(절대경로, `..`, `~`)을 막는다."""
    if not pattern.strip() or pattern.startswith("~") or _DRIVE_OR_ROOT.match(pattern):
        return False
    # 조각 단위가 아니라 문자열 전체에서 막는다 — `{..,x}/**` 같은 중괄호 확장 우회 방지.
    return ".." not in pattern
