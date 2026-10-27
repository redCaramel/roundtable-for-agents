"""대화 설정·메시지 데이터 모델."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_AGENTS = 2
MAX_AGENTS = 6
DEFAULT_MAX_TURNS = 30


class TurnMode(StrEnum):
    ROUND_ROBIN = "round_robin"
    MODERATOR = "moderator"


class EndPolicy(StrEnum):
    UNANIMOUS = "unanimous"  # 전원 동의
    ANY = "any"  # 1명 제안 즉시 종료
    MODERATOR = "moderator"  # 진행자 선언


class ConversationState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"


class AgentConfig(BaseModel):
    name: str
    model: str = "sonnet"
    color: str = "#4a90d9"
    avatar_path: str | None = None
    # 해당 에이전트만 보는 지시사항 (다른 에이전트에게 공개되지 않음)
    instructions: str = ""

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("에이전트 이름이 비어 있습니다.")
        return v


class SessionConfig(BaseModel):
    topic: str
    agents: list[AgentConfig]
    turn_mode: TurnMode = TurnMode.ROUND_ROBIN
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1)
    end_policy: EndPolicy = EndPolicy.UNANIMOUS
    moderator_model: str = "haiku"

    @model_validator(mode="after")
    def _check_agents(self) -> SessionConfig:
        if not MIN_AGENTS <= len(self.agents) <= MAX_AGENTS:
            raise ValueError(f"에이전트는 {MIN_AGENTS}~{MAX_AGENTS}명이어야 합니다.")
        names = [a.name for a in self.agents]
        if len(set(names)) != len(names):
            raise ValueError("에이전트 이름이 중복됩니다.")
        return self

    @property
    def agent_names(self) -> list[str]:
        return [a.name for a in self.agents]


MessageKind = Literal["agent", "user", "system"]


class Message(BaseModel):
    kind: MessageKind
    text: str
    # kind="agent"일 때 발언자 이름
    speaker: str | None = None
    # 에이전트 발언이 몇 번째 턴인지 (1부터)
    turn: int | None = None
    created_at: datetime = Field(default_factory=datetime.now)
