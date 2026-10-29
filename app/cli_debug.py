"""GUI 없이 엔진만 돌려보는 콘솔 도구.

python -m app.cli_debug                          # 기본 주제로 2명 대화
python -m app.cli_debug chat --agents 철수,영희,민수 --topic "점심 메뉴 정하기" --max-turns 6
python -m app.cli_debug chat --mode moderator --end-policy moderator   # 진행자 지목 + 진행자 선언
python -m app.cli_debug smoke "안녕, 자기소개 한 줄 해줘"   # 에이전트 1명 질의 (M0)
python -m app.cli_debug list                                  # 저장된 대화 목록
python -m app.cli_debug resume <폴더 이름> --turns 4            # 저장된 대화 이어하기
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.agent_runner import AgentRunner, ensure_subscription_auth
from app.core.events import (
    Event,
    ModeratorDecided,
    SystemNotice,
    TextDelta,
    ToolActivity,
    TurnCompleted,
    TurnStarted,
)
from app.core.models import AgentConfig, EndPolicy, SessionConfig, TurnMode
from app.core.orchestrator import Orchestrator
from app.core.session_factory import build_orchestrator, load_orchestrator
from app.core.storage import list_sessions, new_session_dir

ROOT = Path(__file__).resolve().parent.parent
CONVERSATIONS = ROOT / "conversations"
LOGIN_HINT = (
    "Claude 구독 계정으로 로그인되어 있는지 확인하세요. "
    "터미널에서 `claude`를 실행한 뒤 /login 으로 로그인합니다 (최초 1회)."
)
DEFAULT_TOPIC = (
    "주말에 할 만한 취미 하나를 함께 골라라. 서로 의견을 주고받고, 마지막엔 하나로 합의한다."
)


async def smoke(prompt: str, model: str) -> int:
    runner = AgentRunner(
        name="테스트",
        model=model,
        system_prompt="당신은 '테스트'라는 이름의 참가자다. 짧고 친절하게 한국어로 답한다.",
        cwd=CONVERSATIONS / "_smoke" / "agents" / "테스트",
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


def _print_event(event: Event) -> None:
    if isinstance(event, TurnStarted):
        print(f"\n[턴 {event.turn}] {event.agent}: ", end="", flush=True)
    elif isinstance(event, TextDelta):
        print(event.text, end="", flush=True)
    elif isinstance(event, ToolActivity):
        mark = "✗ 거부" if event.denied else "·"
        detail = json.dumps(event.tool_input, ensure_ascii=False)[:120]
        print(f"\n    {mark} {event.tool} {detail}", flush=True)
    elif isinstance(event, TurnCompleted):
        print()
    elif isinstance(event, SystemNotice):
        print(f"\n[시스템] {event.message.text}")
    elif isinstance(event, ModeratorDecided):
        tag = "진행자(판단 실패→순서대로)" if event.fallback else "진행자"
        print(f"\n  ({tag} → {event.next or '종료'}: {event.reason})", flush=True)


async def chat(config: SessionConfig) -> int:
    topic, names, max_turns = config.topic, config.agent_names, config.max_turns
    session_dir = new_session_dir(CONVERSATIONS, topic)
    orch = build_orchestrator(config, session_dir, _print_event)
    print(f"주제: {topic}\n참가자: {', '.join(names)} · 최대 {max_turns}턴 · 폴더: {session_dir}")
    await _run_and_report(orch)
    return 0


async def _run_and_report(orch: Orchestrator) -> None:
    try:
        await orch.run()
    finally:
        await orch.close()
    total_in = sum(i for i, _ in orch.usage.values())
    total_out = sum(o for _, o in orch.usage.values())
    print(f"\n--- 상태={orch.state.value} 턴={orch.turn} tokens(in/out)={total_in}/{total_out}")


def list_saved() -> int:
    for info in list_sessions(CONVERSATIONS):
        print(
            f"{info.path.name}\n    {info.state.value} · 턴 {info.turn}/{info.max_turns} · "
            f"{', '.join(info.agent_names)} · {info.updated_at:%Y-%m-%d %H:%M}"
        )
    return 0


async def resume(name: str, turns: int, say: str | None) -> int:
    orch = await load_orchestrator(CONVERSATIONS / name, _print_event)
    print(f"불러옴: {name} (턴 {orch.turn}/{orch.max_turns}, 메시지 {len(orch.transcript)}개)")
    orch.max_turns = orch.turn + turns  # 지금부터 turns턴만 더 진행한다
    orch.save()
    if say:
        await orch.send_user_message(say)
        print(f"[사용자] {say}")
    await _run_and_report(orch)
    return 0


def main() -> int:
    # Windows 콘솔에서 한글이 깨지지 않게 한다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    removed = ensure_subscription_auth()
    if removed:
        print(f"[안내] 구독 로그인만 사용하므로 {', '.join(removed)} 를 이 실행에서 무시합니다.")

    parser = argparse.ArgumentParser(description="엔진 디버그 콘솔")
    sub = parser.add_subparsers(dest="command")
    p_chat = sub.add_parser("chat", help="에이전트 여러 명 대화")
    p_chat.add_argument("--agents", default="철수,영희", help="쉼표로 구분한 이름 (2~6명)")
    p_chat.add_argument("--topic", default=DEFAULT_TOPIC)
    p_chat.add_argument("--max-turns", type=int, default=4)
    p_chat.add_argument("--model", default="haiku")
    p_chat.add_argument("--mode", choices=[m.value for m in TurnMode], default="round_robin")
    p_chat.add_argument("--end-policy", choices=[p.value for p in EndPolicy], default="unanimous")
    p_chat.add_argument("--moderator-model", default="haiku")
    sub.add_parser("list", help="저장된 대화 목록")
    p_resume = sub.add_parser("resume", help="저장된 대화 이어하기")
    p_resume.add_argument("name", help="conversations/ 아래 폴더 이름")
    p_resume.add_argument("--turns", type=int, default=4, help="이어서 진행할 턴 수")
    p_resume.add_argument("--say", help="이어가기 전에 보낼 사용자 메시지")
    p_smoke = sub.add_parser("smoke", help="에이전트 1명 질의·스트리밍 확인")
    p_smoke.add_argument("prompt", nargs="?", default="안녕하세요! 한 문장으로 자기소개 해주세요.")
    p_smoke.add_argument("--model", default="haiku")
    args = parser.parse_args(sys.argv[1:] or ["chat"])

    if args.command == "list":
        return list_saved()
    if args.command == "resume":
        return asyncio.run(resume(args.name, args.turns, args.say))
    if args.command == "smoke":
        return asyncio.run(smoke(args.prompt, args.model))
    names = [n.strip() for n in args.agents.split(",") if n.strip()]
    try:
        config = SessionConfig(
            topic=args.topic,
            agents=[AgentConfig(name=n, model=args.model) for n in names],
            max_turns=args.max_turns,
            turn_mode=TurnMode(args.mode),
            end_policy=EndPolicy(args.end_policy),
            moderator_model=args.moderator_model,
        )
        return asyncio.run(chat(config))
    except ValueError as e:  # 설정 검증 실패
        print(f"오류: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
