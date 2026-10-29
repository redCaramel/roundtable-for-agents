"""엔진 → GUI 이벤트. GUI 쪽 어댑터가 이를 Qt Signal로 옮긴다 (plan.md 3장)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.models import ConversationState, Message


@dataclass(frozen=True)
class TurnStarted:
    agent: str
    turn: int


@dataclass(frozen=True)
class TextDelta:
    agent: str
    text: str


@dataclass(frozen=True)
class ToolActivity:
    """도구 호출 1건 (활동 카드용). denied면 샌드박스가 막은 시도다."""

    agent: str
    tool: str
    tool_input: dict[str, Any]
    denied: bool
    reason: str = ""


@dataclass(frozen=True)
class TurnCompleted:
    message: Message


@dataclass(frozen=True)
class TurnInterrupted:
    """발언이 중단되어 폐기되었다. reason: "user"(사용자 개입) / "pause" / "stop"."""

    agent: str
    reason: str


@dataclass(frozen=True)
class UserMessageAdded:
    message: Message


@dataclass(frozen=True)
class ModeratorDecided:
    """진행자의 다음 발언자 선택. 채팅에는 남기지 않고 활동 표시에만 쓴다 (plan.md 4.5)."""

    next: str | None
    reason: str
    fallback: bool = False  # 진행자 판단 실패로 라운드로빈 대체
    end: bool = False


@dataclass(frozen=True)
class SystemNotice:
    message: Message


@dataclass(frozen=True)
class StateChanged:
    state: ConversationState


@dataclass(frozen=True)
class UsageUpdated:
    agent: str
    input_tokens: int
    output_tokens: int


Event = (
    TurnStarted
    | TextDelta
    | ToolActivity
    | TurnCompleted
    | TurnInterrupted
    | UserMessageAdded
    | ModeratorDecided
    | SystemNotice
    | StateChanged
    | UsageUpdated
)
EventSink = Callable[[Event], None]
