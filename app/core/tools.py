"""커스텀 도구 (plan.md 4.4).

SDK 의존을 agent_runner.py에 모으기 위해, 여기서는 도구 정의(이름·설명·입력 스키마)와
처리 로직만 둔다. agent_runner가 이를 SDK 인프로세스 MCP 서버로 감싼다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.access_control import AccessControl, AccessError
from app.core.end_vote import EndVote, EndVoteError
from app.core.models import EndPolicy


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolOutcome]


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}

Handler = Callable[[dict[str, Any]], ToolOutcome]


def _guarded(fn: Callable[[dict[str, Any]], str], *domain_errors: type[Exception]) -> Handler:
    """예외를 에이전트에게 돌려줄 오류 결과로 바꾼다. 도메인 오류는 메시지를 그대로 쓴다."""

    def run(args: dict[str, Any]) -> ToolOutcome:
        try:
            return ToolOutcome(fn(args))
        except domain_errors as e:
            return ToolOutcome(str(e), is_error=True)
        except (KeyError, TypeError, ValueError) as e:
            return ToolOutcome(f"입력값이 잘못되었습니다: {e}", is_error=True)

    return run


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name}는 true/false여야 합니다.")
    return value


def build_end_tools(end_vote: EndVote, agent_name: str) -> list[ToolSpec]:
    """종료 제안·투표 도구. 정책에 따라 vote_end는 넣지 않는다."""

    def propose(args: dict[str, Any]) -> str:
        return end_vote.propose(agent_name, str(args.get("reason", "")))

    def vote(args: dict[str, Any]) -> str:
        agree = _require_bool(args["agree"], "agree")
        return end_vote.vote(agent_name, agree, str(args.get("reason", "")))

    specs = [
        ToolSpec(
            "propose_end",
            "대화를 끝내자고 제안한다. 주제의 목적을 이뤘거나 더 할 이야기가 없을 때 쓴다. "
            "제안은 대화에 공개된다.",
            _obj({"reason": _STR}, ["reason"]),
            _guarded(propose, EndVoteError),
        )
    ]
    if end_vote.policy == EndPolicy.UNANIMOUS:
        specs.append(
            ToolSpec(
                "vote_end",
                "진행 중인 종료 제안에 찬성(agree=true) 또는 반대(agree=false)를 표시한다. "
                "한 명이라도 반대하면 부결되고, 전원이 찬성하면 대화가 끝난다.",
                _obj({"agree": {"type": "boolean"}, "reason": _STR}, ["agree"]),
                _guarded(vote, EndVoteError),
            )
        )
    return specs


def build_access_tools(
    acl: AccessControl,
    agent_name: str,
    current_turn: Callable[[], int],
    folder_of: Callable[[str], str],
) -> list[ToolSpec]:
    """agent_name 한 명이 쓰는 열람 권한 도구들. folder_of(이름)은 그 폴더의 읽기 경로."""

    def guarded(fn: Callable[[dict[str, Any]], str]) -> Handler:
        return _guarded(fn, AccessError)

    def request(args: dict[str, Any]) -> str:
        req = acl.request(
            agent_name, str(args["target"]), str(args.get("reason", "")), current_turn()
        )
        return (
            f"요청 #{req.id}를 보냈습니다. 요청은 대화에 공개되며, {req.target}가 자기 차례에 "
            "허가 또는 거부합니다. 결과는 다음 대화 업데이트로 전달됩니다."
        )

    def respond(args: dict[str, Any]) -> str:
        grant = _require_bool(args["grant"], "grant")
        req = acl.respond(agent_name, int(args["request_id"]), grant, str(args.get("reason", "")))
        if grant:
            return (
                f"{req.requester}에게 당신 폴더의 읽기 권한을 주었습니다. "
                "revoke_access로 언제든 철회할 수 있습니다."
            )
        return f"{req.requester}의 요청을 거부했습니다."

    def revoke(args: dict[str, Any]) -> str:
        reader = str(args["agent"])
        acl.revoke(agent_name, reader)
        return f"{reader}의 열람 권한을 철회했습니다."

    def list_access(_: dict[str, Any]) -> str:
        lines = ["[내가 읽을 수 있는 다른 폴더]"]
        owners = acl.readable_owners(agent_name)
        lines += [f"- {o}: 경로 {folder_of(o)}" for o in owners] or ["- 없음"]
        lines.append("[내 폴더를 읽을 수 있는 참가자]")
        lines += [f"- {r}" for r in acl.readers_of(agent_name)] or ["- 없음"]
        lines.append("[나에게 온 대기 중 요청]")
        lines += [
            f"- #{r.id} {r.requester} (사유: {r.reason})" for r in acl.pending_for(agent_name)
        ] or ["- 없음"]
        lines.append("[내가 보낸 대기 중 요청]")
        lines += [f"- #{r.id} → {r.target}" for r in acl.pending_from(agent_name)] or ["- 없음"]
        return "\n".join(lines)

    return [
        ToolSpec(
            "request_folder_access",
            "다른 참가자의 개인 폴더를 읽기 위해 열람을 요청한다. 요청은 대화에 공개되고, "
            "대상 참가자가 자기 차례에 허가/거부한다.",
            _obj({"target": _STR, "reason": _STR}, ["target", "reason"]),
            guarded(request),
        ),
        ToolSpec(
            "respond_access_request",
            "나에게 온 폴더 열람 요청에 허가(grant=true) 또는 거부(grant=false)로 응답한다. "
            "허가는 읽기 전용이며 철회할 때까지 유지된다.",
            _obj(
                {"request_id": {"type": "integer"}, "grant": {"type": "boolean"}, "reason": _STR},
                ["request_id", "grant"],
            ),
            guarded(respond),
        ),
        ToolSpec(
            "revoke_access",
            "전에 내 폴더 열람을 허가해 준 참가자의 권한을 철회한다.",
            _obj({"agent": _STR}, ["agent"]),
            guarded(revoke),
        ),
        ToolSpec(
            "list_my_access",
            "내가 읽을 수 있는 폴더(와 그 경로), 내 폴더를 읽을 수 있는 참가자, "
            "대기 중인 요청을 본다.",
            _obj({}, []),
            guarded(list_access),
        ),
    ]
