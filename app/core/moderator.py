"""진행자 에이전트 (plan.md 4.5).

대화에 참여하지 않고 다음 발언자만 고른다. 도구 없이 JSON 한 덩어리로 답한다.
SDK 세션 관리는 AgentRunner에 맡기고(도구·샌드박스 없이), 여기서는 판단 결과 해석만 한다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.core.agent_runner import AgentRunner, TurnResult
from app.core.models import SessionConfig
from app.core.prompts import build_moderator_system_prompt

MODERATOR_NAME = "진행자"
# 사용량 표에서 에이전트 이름과 겹치지 않게 쓰는 키
MODERATOR_USAGE_KEY = "(진행자)"

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class ModeratorError(ValueError):
    """진행자 응답을 해석할 수 없다. 오케스트레이터는 라운드로빈으로 대체한다.

    usage에는 실패한 응답의 사용량이 담긴다 (실패해도 한도는 썼으므로 집계한다).
    """

    def __init__(self, message: str, usage: TurnResult | None = None) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class ModeratorDecision:
    next: str | None
    reason: str
    end: bool
    usage: TurnResult | None = None


def parse_decision(text: str, names: Iterable[str]) -> tuple[str | None, str, bool]:
    """진행자 응답에서 {"next", "reason", "end"}를 꺼낸다. 앞뒤 잡담이나 코드 블록은 허용한다."""
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise ModeratorError(f"JSON이 없습니다: {text[:80]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ModeratorError(f"JSON 해석 실패: {e}") from None
    if not isinstance(data, dict):
        raise ModeratorError("JSON 객체가 아닙니다.")
    end = data.get("end", False)
    if not isinstance(end, bool):
        raise ModeratorError("end는 true/false여야 합니다.")
    reason = str(data.get("reason", "")).strip()
    nxt = data.get("next")
    if isinstance(nxt, str):
        nxt = nxt.strip().lstrip("@")
    if nxt not in list(names):
        if end:
            return None, reason, True  # 끝낼 때는 다음 발언자가 없어도 된다
        raise ModeratorError(f"참가자가 아닌 next: {nxt!r}")
    return nxt, reason, end


class Moderator:
    def __init__(self, config: SessionConfig, cwd: Path) -> None:
        self.names = config.agent_names
        self.runner = AgentRunner(
            name=MODERATOR_NAME,
            model=config.moderator_model,
            system_prompt=build_moderator_system_prompt(config),
            cwd=cwd,
        )

    async def decide(self, prompt: str) -> ModeratorDecision:
        result = await self.runner.run_turn(prompt)
        if result.is_error:
            raise ModeratorError(f"진행자 응답 오류: {result.text[:80]}", usage=result)
        try:
            nxt, reason, end = parse_decision(result.text, self.names)
        except ModeratorError as e:
            raise ModeratorError(str(e), usage=result) from None
        return ModeratorDecision(nxt, reason, end, usage=result)

    async def interrupt(self) -> None:
        await self.runner.interrupt()

    async def disconnect(self) -> None:
        await self.runner.disconnect()
