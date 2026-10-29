"""대화 폴더 경로 관리와 자동 저장 / 불러오기 (plan.md 5.1, 8장).

conversations/<세션>/
  session.json      설정 + 진행 상태 + 각 에이전트 SDK session_id (스냅샷, 매번 통째로 갈아씀)
  transcript.jsonl  확정된 메시지 로그 (한 줄에 하나, 덧붙이기만 함)
  acl.json          열람 허가 목록 (AccessControl이 직접 저장)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.models import ConversationState, Message, SessionConfig

log = logging.getLogger(__name__)

SESSION_FILE = "session.json"
TRANSCRIPT_FILE = "transcript.jsonl"
ACL_FILE = "acl.json"
SNAPSHOT_VERSION = 1

# Windows 금지 문자와 제어 문자
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 예약 장치 이름
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
_MAX_LEN = 40


def safe_dirname(name: str, fallback: str = "untitled") -> str:
    """사용자 입력(에이전트 이름·주제)을 Windows에서 안전한 폴더명으로 바꾼다. 한글은 유지한다."""
    cleaned = _FORBIDDEN.sub("_", name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")[:_MAX_LEN].rstrip(" ._")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def new_session_dir(root: Path, topic: str, now: datetime | None = None) -> Path:
    """conversations/<YYYY-MM-DD_HHMMSS>_<주제>/ 를 만든다 (plan.md 5.1)."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    first_line = topic.strip().splitlines()[0] if topic.strip() else ""
    base = root / f"{stamp}_{safe_dirname(first_line, 'conversation')}"
    path = base
    n = 2
    while path.exists():
        path = base.with_name(f"{base.name}_{n}")
        n += 1
    (path / "agents").mkdir(parents=True)
    return path


def agent_dir(session_dir: Path, agent_name: str) -> Path:
    return session_dir / "agents" / safe_dirname(agent_name, "agent")


# ── 스냅샷 ───────────────────────────────────────────
class SessionSnapshot(BaseModel):
    """이어하기에 필요한 진행 상태 전부. 메시지 본문은 transcript.jsonl에 따로 둔다."""

    version: int = SNAPSHOT_VERSION
    config: SessionConfig
    state: ConversationState = ConversationState.IDLE
    turn: int = 0
    max_turns: int
    next_index: int = 0
    cursors: dict[str, int] = Field(default_factory=dict)
    spoken: list[str] = Field(default_factory=list)
    interrupted: list[str] = Field(default_factory=list)
    forced_next: str | None = None
    mod_cursor: int = 0
    agent_sessions: dict[str, str | None] = Field(default_factory=dict)
    moderator_session: str | None = None
    usage: dict[str, tuple[int, int]] = Field(default_factory=dict)
    token_limit: int | None = None
    end_vote: dict[str, Any] = Field(default_factory=dict)
    # 저장 시점의 transcript 길이. 로그가 이보다 길면(쓰다 끊김 등) 잘라 맞춘다.
    message_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class SessionInfo(BaseModel):
    """대화 목록에 보여줄 요약."""

    path: Path
    topic: str
    state: ConversationState
    turn: int
    max_turns: int
    agent_names: list[str]
    updated_at: datetime


REPLACE_RETRIES = 20
REPLACE_RETRY_SECONDS = 0.025


def _read_text(path: Path) -> str:
    """교체 중인 파일을 읽다 PermissionError가 나면(Windows) 잠깐씩 다시 시도한다."""
    for attempt in range(REPLACE_RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)
    raise AssertionError("unreachable")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows에서는 다른 스레드(대화 목록 새로고침 등)가 대상 파일을 읽는 순간 교체가
    # PermissionError로 실패한다. 읽기는 금방 끝나므로 잠깐씩 다시 시도한다.
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


class SessionStore:
    """한 대화의 자동 저장. 쓰기는 단일 스레드에서 순서대로 처리해 이벤트 루프를 막지 않는다."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-store")
        self._last: Future[None] | None = None
        self._closed = False

    @property
    def session_path(self) -> Path:
        return self.session_dir / SESSION_FILE

    @property
    def transcript_path(self) -> Path:
        return self.session_dir / TRANSCRIPT_FILE

    def _submit(self, fn: Any, *args: Any) -> None:
        if self._closed:
            log.warning("닫힌 저장소에 쓰기 요청 — 무시함: %s", self.session_dir)
            return

        def run() -> None:
            try:
                fn(*args)
            except OSError:
                log.exception("대화 저장 실패: %s", self.session_dir)

        self._last = self._executor.submit(run)

    def append_message(self, msg: Message) -> None:
        line = msg.model_dump_json() + "\n"
        self._submit(self._append_line, line)

    def _append_line(self, line: str) -> None:
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def save_snapshot(self, snapshot: SessionSnapshot) -> None:
        text = snapshot.model_dump_json(indent=2)
        self._submit(_atomic_write, self.session_path, text)

    def flush(self) -> None:
        """밀린 쓰기가 끝날 때까지 기다린다 (테스트·종료 직전용, 블로킹)."""
        if self._last is not None:
            self._last.result()

    async def aflush(self) -> None:
        if self._last is not None:
            await asyncio.wrap_future(self._last)

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=True)

    # ── 불러오기 (블로킹 — 호출자가 스레드에서 부른다) ──
    def load(self) -> tuple[SessionSnapshot, list[Message]]:
        snapshot = SessionSnapshot.model_validate_json(_read_text(self.session_path))
        messages: list[Message] = []
        if self.transcript_path.exists():
            for n, line in enumerate(_read_text(self.transcript_path).splitlines()):
                if not line.strip():
                    continue
                try:
                    messages.append(Message.model_validate_json(line))
                except ValueError:
                    # 저장 도중 끊긴 마지막 줄 등은 버린다
                    log.warning("transcript %d번째 줄을 읽을 수 없어 건너뜀", n + 1)
        if len(messages) > snapshot.message_count:
            # 스냅샷보다 앞서 기록된 메시지는 진행 상태(커서 등)와 어긋나므로 버린다
            messages = messages[: snapshot.message_count]
        return snapshot, messages


def list_sessions(root: Path) -> list[SessionInfo]:
    """저장된 대화 목록 (최근 것부터). 깨진 폴더는 건너뛴다. 블로킹."""
    infos: list[SessionInfo] = []
    if not root.exists():
        return infos
    for d in root.iterdir():
        path = d / SESSION_FILE
        if not path.is_file():
            continue
        try:
            data = json.loads(_read_text(path))
            snap = SessionSnapshot.model_validate(data)
        except (OSError, ValueError):
            log.warning("대화 정보를 읽을 수 없음: %s", d)
            continue
        infos.append(
            SessionInfo(
                path=d,
                topic=snap.config.topic.strip().splitlines()[0] if snap.config.topic else "",
                state=snap.state,
                turn=snap.turn,
                max_turns=snap.max_turns,
                agent_names=snap.config.agent_names,
                updated_at=snap.updated_at,
            )
        )
    infos.sort(key=lambda i: i.updated_at, reverse=True)
    return infos
