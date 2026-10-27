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


class MissingApiKeyError(RuntimeError):
    """ANTHROPIC_API_KEY가 설정되지 않았다."""


@dataclass
class TurnResult:
    text: str
    session_id: str | None
    cost_usd: float | None
    is_error: bool


def require_api_key() -> str:
    """종량제 API 키를 강제한다. 없으면 CLI 로그인(구독)으로 몰래 넘어가지 않도록 막는다."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY가 없습니다. .env.example을 .env로 복사하고 키를 입력하세요."
        )
    return key


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
            env={"ANTHROPIC_API_KEY": require_api_key()},
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
                is_error = msg.is_error
                final_text = msg.result or ""

        return TurnResult(
            text=final_text or "".join(collected),
            session_id=self.session_id,
            cost_usd=cost,
            is_error=is_error,
        )


def _text_delta(event: dict) -> str | None:
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta":
        return delta.get("text")
    return None
