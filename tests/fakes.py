"""테스트용 가짜 에이전트."""

import asyncio
from collections.abc import Callable

from app.core.agent_runner import TurnResult


class FakeAgent:
    def __init__(self, name: str, fail_on: set[int] | None = None) -> None:
        self.name = name
        self.prompts: list[str] = []
        self.fail_on = fail_on or set()
        self.disconnected = False

    async def run_turn(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_tool=None,
    ):
        self.prompts.append(prompt)
        n = len(self.prompts)
        if n in self.fail_on:
            raise RuntimeError("네트워크 오류")
        text = f"{self.name}의 {n}번째 발언"
        if on_text:
            on_text(text)
        return TurnResult(
            text=text,
            session_id="s",
            cost_usd=None,
            input_tokens=10,
            output_tokens=5,
            is_error=False,
        )

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


class SlowAgent:
    """글자를 조금씩 흘려보내는 에이전트. interrupt()하면 SDK처럼 오류 결과로 일찍 끝난다.

    stuck=True면 interrupt를 무시해 오케스트레이터의 취소 경로를 시험한다.
    """

    def __init__(self, name: str, chunks: int = 50, stuck: bool = False) -> None:
        self.name = name
        self.chunks = chunks
        self.stuck = stuck
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.cancelled = False
        self.disconnected = False
        self._interrupted = asyncio.Event()

    async def run_turn(self, prompt: str, on_text=None, on_tool=None) -> TurnResult:
        self.prompts.append(prompt)
        self._interrupted.clear()
        self.started.set()
        try:
            for i in range(self.chunks):
                if self._interrupted.is_set() and not self.stuck:
                    return TurnResult("", "s", None, 1, 1, is_error=True)
                if on_text:
                    on_text(f"{i} ")
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.started.clear()
        return TurnResult(f"{self.name} 발언 완료", "s", None, 10, 5, is_error=False)

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def disconnect(self) -> None:
        self.disconnected = True
