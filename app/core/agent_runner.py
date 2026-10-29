"""ClaudeSDKClient 래퍼. Claude Agent SDK 호출은 이 파일(+ moderator.py)에만 둔다."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import McpSdkServerConfig

from app.core.sandbox import CUSTOM_MCP_SERVER, Decision, Sandbox
from app.core.tools import ToolSpec

log = logging.getLogger(__name__)

# 도구 호출 1건이 샌드박스 판정을 받을 때마다 불린다: (도구 이름, 입력, 판정)
ToolCallback = Callable[[str, dict[str, Any], Decision], None]

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
        tools: list[str] | None = None,
        sandbox: Sandbox | None = None,
        custom_tools: list[ToolSpec] | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.tools = tools or []
        if self.tools and sandbox is None:
            raise ValueError("도구를 열려면 샌드박스가 필요합니다.")
        self.sandbox = sandbox
        self.custom_tools = custom_tools or []
        if self.custom_tools and sandbox is None:
            raise ValueError("도구를 열려면 샌드박스가 필요합니다.")
        self.session_id = resume_session_id
        self._client: ClaudeSDKClient | None = None
        self._on_tool: ToolCallback | None = None
        # 응답을 끝까지 읽지 못하고 턴이 취소되면 True. 남은 메시지가 다음 턴에 섞이지 않게
        # 다음 연결 때 세션을 resume으로 새로 맺는다.
        self._stale = False

    def build_options(self) -> ClaudeAgentOptions:
        # SDK는 이 프로세스의 환경변수를 CLI에 넘기므로, 키가 남아 있으면 여기서 막는다.
        _assert_subscription_auth()
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=self.model,
            cwd=str(self.cwd),
            # 쓸 수 있는 내장 도구 자체를 한정한다.
            tools=list(self.tools),
            # 자동 허용(allowed_tools)은 비운다. 모든 호출은 아래 PreToolUse 훅에서 판정한다.
            # can_use_tool은 CLI가 '물어볼' 때만 불려 우회될 수 있어 판정에 쓰지 않는다.
            allowed_tools=[],
            disallowed_tools=list(DISALLOWED_TOOLS),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])]},
            # 훅을 통과하고도 권한 확인이 필요한 경우가 생기면 전부 거부한다 (안전망).
            can_use_tool=_deny_permission_prompt,
            # 사용자 PC의 CLAUDE.md·설정이 섞이지 않게 한다.
            setting_sources=[],
            # 로그인 계정의 claude.ai 커넥터(Gmail·Notion 등)가 MCP 도구로 붙지 않게 한다.
            # 안 막으면 에이전트가 사용자 계정에 접근할 수 있고, 매 턴 입력이 ~15만 토큰 늘어난다.
            # 우리 커스텀 도구는 mcp_servers로 명시적으로 넘기므로 그대로 쓸 수 있다.
            mcp_servers=self._mcp_servers(),
            strict_mcp_config=True,
            include_partial_messages=True,
            resume=self.session_id,
        )

    def _mcp_servers(self) -> dict[str, McpSdkServerConfig]:
        if not self.custom_tools:
            return {}
        server = create_sdk_mcp_server(
            name=CUSTOM_MCP_SERVER, tools=[_to_sdk_tool(t) for t in self.custom_tools]
        )
        return {CUSTOM_MCP_SERVER: server}

    async def connect(self) -> None:
        if self._client is not None and self._stale:
            await self.disconnect()
        if self._client is not None:
            return
        self._stale = False
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._client = ClaudeSDKClient(options=self.build_options())
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            await client.disconnect()
        except Exception:  # 이미 끊긴 연결 등 — 정리만 하면 된다
            log.warning("[%s] 연결 종료 중 오류", self.name, exc_info=True)

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def _pre_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        """모든 도구 호출의 관문. 샌드박스 판정이 곧 허용/거부다."""
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        if self.sandbox is None:
            decision = Decision(False, "이 에이전트에게는 도구가 열려 있지 않습니다.")
        else:
            decision = self.sandbox.check(tool_name, tool_input)
        if not decision.allowed:
            log.info(
                "[%s] 도구 거부: %s %s — %s", self.name, tool_name, tool_input, decision.reason
            )
        if self._on_tool is not None:
            self._on_tool(tool_name, tool_input, decision)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if decision.allowed else "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    async def run_turn(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_tool: ToolCallback | None = None,
    ) -> TurnResult:
        """프롬프트 하나를 보내고 응답을 스트리밍한다. 최종 텍스트가 이 에이전트의 '발언'이다."""
        await self.connect()
        assert self._client is not None
        self._on_tool = on_tool
        try:
            return await self._run_turn(prompt, on_text)
        except asyncio.CancelledError:
            self._stale = True
            raise
        finally:
            self._on_tool = None

    async def _run_turn(self, prompt: str, on_text: Callable[[str], None] | None) -> TurnResult:
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
                # 캐시 적중·생성분도 한도에서 차감되므로 입력 토큰에 합산한다.
                input_tokens = sum(
                    usage.get(k) or 0
                    for k in (
                        "input_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
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


def _to_sdk_tool(spec: ToolSpec):  # noqa: ANN202 — SDK 제네릭 타입
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        outcome = spec.handler(args)
        return {
            "content": [{"type": "text", "text": outcome.text}],
            "is_error": outcome.is_error,
        }

    return tool(spec.name, spec.description, spec.input_schema)(handler)


async def _deny_permission_prompt(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultDeny:
    return PermissionResultDeny(message=f"'{tool_name}' 사용 권한이 없습니다.")


def _text_delta(event: dict) -> str | None:
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta":
        return delta.get("text")
    return None
