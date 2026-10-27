"""GUI 없이 엔진만 돌려보는 콘솔 도구.

M0: 에이전트 1명에게 질의하고 응답을 스트리밍으로 출력한다.
    python -m app.cli_debug "안녕, 자기소개 한 줄 해줘"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.agent_runner import AgentRunner, ensure_subscription_auth

ROOT = Path(__file__).resolve().parent.parent
LOGIN_HINT = (
    "Claude 구독 계정으로 로그인되어 있는지 확인하세요. "
    "터미널에서 `claude`를 실행한 뒤 /login 으로 로그인합니다 (최초 1회)."
)


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
        print(
            f"--- session={result.session_id} "
            f"tokens(in/out)={result.input_tokens}/{result.output_tokens} error={result.is_error}"
        )
        if result.is_error:
            print(LOGIN_HINT, file=sys.stderr)
        return 1 if result.is_error else 0
    except Exception as e:  # 로그인 안 됨·만료, CLI 실행 실패 등
        print(f"\n오류: {e}\n{LOGIN_HINT}", file=sys.stderr)
        return 2
    finally:
        await runner.disconnect()


def main() -> int:
    # Windows 콘솔에서 한글이 깨지지 않게 한다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    removed = ensure_subscription_auth()
    if removed:
        print(f"[안내] 구독 로그인만 사용하므로 {', '.join(removed)} 를 이 실행에서 무시합니다.")

    parser = argparse.ArgumentParser(description="엔진 디버그 콘솔")
    parser.add_argument("prompt", nargs="?", default="안녕하세요! 한 문장으로 자기소개 해주세요.")
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args()

    return asyncio.run(smoke(args.prompt, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
