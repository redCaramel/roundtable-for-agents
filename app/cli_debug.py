"""GUI 없이 엔진만 돌려보는 콘솔 도구.

M0: 에이전트 1명에게 질의하고 응답을 스트리밍으로 출력한다.
    python -m app.cli_debug "안녕, 자기소개 한 줄 해줘"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.core.agent_runner import AgentRunner, MissingApiKeyError, require_api_key

ROOT = Path(__file__).resolve().parent.parent


async def smoke(prompt: str, model: str) -> int:
    runner = AgentRunner(
        name="테스트",
        model=model,
        system_prompt="당신은 '테스트'라는 이름의 참가자다. 짧고 친절하게 한국어로 답한다.",
        cwd=ROOT / "conversations" / "_smoke" / "agents" / "테스트",
    )
    try:
        print(f"[{runner.name}] ", end="", flush=True)
        result = await runner.run_turn(prompt, on_text=lambda t: print(t, end="", flush=True))
        print()
        cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "?"
        print(f"--- session={result.session_id} cost={cost} error={result.is_error}")
        return 1 if result.is_error else 0
    finally:
        await runner.disconnect()


def main() -> int:
    # Windows 콘솔에서 한글이 깨지지 않게 한다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="엔진 디버그 콘솔")
    parser.add_argument("prompt", nargs="?", default="안녕하세요! 한 문장으로 자기소개 해주세요.")
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args()

    try:
        require_api_key()
        return asyncio.run(smoke(args.prompt, args.model))
    except MissingApiKeyError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
