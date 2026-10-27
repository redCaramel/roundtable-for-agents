"""엔진 → GUI 이벤트. GUI 쪽 어댑터가 이를 Qt Signal로 옮긴다 (plan.md 3장)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
class TurnCompleted:
    message: Message


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


Event = TurnStarted | TextDelta | TurnCompleted | SystemNotice | StateChanged | UsageUpdated
EventSink = Callable[[Event], None]
