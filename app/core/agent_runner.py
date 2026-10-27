"""ClaudeSDKClient 래퍼. Claude Agent SDK 호출은 이 파일(+ moderator.py)에만 둔다."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

# 샌드박스를 벗어날 수 있는 도구는 전부 막는다 (plan.md 4.2).
DISALLOWED_TOOLS: list[str] = [
    "Bash",
    "BashOutput",
    "KillShell",
    "NotebookEdit",
    "Task",
    "Agent",
]


# 인증은 Claude 구독 계정 로그인(Claude Code 로그인)으로 고정한다 (plan.md 4.2).
# 아래 변수가 환경에 있으면 Claude Code가 구독 대신 API 종량제로 청구하므로 쓰지 않는다.
API_BILLING_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ApiKeyConflictError(RuntimeError):
    """구독 로그인으로 고정했는데 API 키 환경변수가 남아 있다."""


@dataclass
class TurnResult:
    text: str
    session_id: str | None
    # 구독 로그인에서는 실제 청구액이 아니라 참고용 환산값이다. 사용량 표시는 토큰 수 기준.
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    is_error: bool


def ensure_subscription_auth() -> list[str]:
    """이 프로세스 환경에서 API 키 변수를 지워 구독 로그인만 쓰게 한다.

    사용자 PC의 시스템 환경변수는 건드리지 않고 이 프로그램 프로세스 안에서만 지운다.
    앱 진입점에서 가장 먼저 호출한다. 지운 변수 이름 목록을 돌려준다.
    """
    removed = [name for name in API_BILLING_ENV_VARS if name in os.environ]
    for name in removed:
        del os.environ[name]
    return removed


def _assert_subscription_auth() -> None:
    present = [name for name in API_BILLING_ENV_VARS if name in os.environ]
    if present:
        raise ApiKeyConflictError(
            f"{', '.join(present)} 환경변수가 있으면 구독 대신 종량제로 청구됩니다. "
            "앱 시작 시 ensure_subscription_auth()를 먼저 호출하세요."
        )


class AgentRunner:
    """에이전트 1명 = ClaudeSDKClient 세션 1개."""

    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.allowed_tools = allowed_tools or []
        self.session_id = resume_session_id
        self._client: ClaudeSDKClient | None = None

    def build_options(self) -> ClaudeAgentOptions:
        # SDK는 이 프로세스의 환경변수를 CLI에 넘기므로, 키가 남아 있으면 여기서 막는다.
        _assert_subscription_auth()
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=self.model,
            cwd=str(self.cwd),
            # 사용 가능한 내장 도구 자체를 allowed_tools로 한정한다.
            tools=list(self.allowed_tools),
            allowed_tools=list(self.allowed_tools),
            disallowed_tools=list(DISALLOWED_TOOLS),
            # 사용자 PC의 CLAUDE.md·설정이 섞이지 않게 한다.
            setting_sources=[],
            include_partial_messages=True,
            resume=self.session_id,
        )

    async def connect(self) -> None:
        if self._client is not None:
            return
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._client = ClaudeSDKClient(options=self.build_options())
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        await self._client.disconnect()
        self._client = None

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def run_turn(
        self, prompt: str, on_text: Callable[[str], None] | None = None
    ) -> TurnResult:
        """프롬프트 하나를 보내고 응답을 스트리밍한다. 최종 텍스트가 이 에이전트의 '발언'이다."""
        await self.connect()
        assert self._client is not None
        await self._client.query(prompt)

        final_text = ""
        collected: list[str] = []
        cost: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        is_error = False
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                delta = _text_delta(msg.event)
                if delta and on_text is not None:
                    on_text(delta)
            elif isinstance(msg, AssistantMessage):
                collected.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                self.session_id = msg.session_id
                cost = msg.total_cost_usd
                usage = msg.usage or {}
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                is_error = msg.is_error
                final_text = msg.result or ""

        return TurnResult(
            text=final_text or "".join(collected),
            session_id=self.session_id,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            is_error=is_error,
        )


def _text_delta(event: dict) -> str | None:
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta":
        return delta.get("text")
    return None
