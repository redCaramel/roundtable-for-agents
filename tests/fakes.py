"""테스트용 가짜 에이전트."""

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
