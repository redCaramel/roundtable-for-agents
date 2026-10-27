"""대화 폴더 경로 관리. 자동 저장/이어하기는 M7에서 채운다."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

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
