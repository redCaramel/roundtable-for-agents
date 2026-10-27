"""시스템 프롬프트와 턴 업데이트 템플릿 (plan.md 4.1, 4.3)."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.models import AgentConfig, Message, SessionConfig, TurnMode

_TURN_MODE_TEXT = {
    TurnMode.ROUND_ROBIN: "정해진 순서대로 돌아가며 한 명씩 발언한다.",
    TurnMode.MODERATOR: "진행자가 매번 다음 발언자를 지목한다.",
}


def build_system_prompt(config: SessionConfig, agent: AgentConfig) -> str:
    others = [n for n in config.agent_names if n != agent.name]
    parts = [
        "# 역할",
        f"당신은 '{agent.name}'이다. 여러 참가자가 함께하는 대화에 한 명의 참가자로 참여한다.",
        f"다른 참가자: {', '.join(others)}",
        f"진행 방식: {_TURN_MODE_TEXT[config.turn_mode]}",
        "",
        "# 주제 / 규칙",
        config.topic.strip(),
    ]
    if agent.instructions.strip():
        parts += [
            "",
            "# 개인 지시사항 (당신만 알고 있는 내용이다. 다른 참가자에게 그대로 밝히지 않는다)",
            agent.instructions.strip(),
        ]
    parts += [
        "",
        "# 대화 규칙",
        "- 차례가 오면 '[대화 업데이트]'로 지난 차례 이후 나온 메시지를 받는다.",
        "- 당신 자신의 발언만 한다. 다른 참가자의 대사를 대신 쓰거나 대화를 지어내지 않는다.",
        f"- 발언 앞에 '{agent.name}:' 같은 이름 접두를 붙이지 않는다. 내용만 쓴다.",
        "- 한 번의 발언은 보통 3~6문장 정도로 한다. 주제/규칙에서 길이를 정하면 그것을 따른다.",
        "- '[사용자]' 메시지는 대화를 지켜보는 사용자의 개입이다. 최우선으로 반영한다.",
        "- 응답 언어는 주제/규칙에서 정하지 않았다면 한국어로 한다.",
        "",
        "# 도구 규칙",
        "- 현재 작업 폴더가 당신의 개인 폴더다. 메모·자료·검색 결과를 파일로 저장할 수 있다.",
        "- 파일 쓰기는 개인 폴더 안에서만 된다. 다른 참가자의 폴더는 허가 없이 읽을 수 없다.",
        "- 다른 참가자의 폴더를 읽으려면 request_folder_access로 요청한다. 요청은 대화에 공개된다.",
        "- 허가를 받으면 list_my_access로 그 폴더의 경로를 확인해 읽는다. 허가는 읽기 전용이다.",
        "- 나에게 온 요청은 respond_access_request로 허가/거부한다. "
        "준 허가는 revoke_access로 철회한다.",
        "- 웹 검색(WebSearch)과 웹 페이지 읽기(WebFetch)를 쓸 수 있다. 필요할 때만 쓴다.",
        "- 도구 사용 과정은 다른 참가자에게 보이지 않는다. 결과는 발언으로 직접 전한다.",
    ]
    return "\n".join(parts)


def format_message(msg: Message) -> str:
    if msg.kind == "agent":
        return f"{msg.speaker}: {msg.text}"
    if msg.kind == "user":
        return f"[사용자]: {msg.text}"
    return f"[시스템] {msg.text}"


def build_turn_update(
    agent_name: str,
    new_messages: Sequence[Message],
    is_first_turn: bool,
    reminders: Sequence[str] = (),
) -> str:
    lines: list[str] = []
    if new_messages:
        lines.append("[대화 업데이트]")
        lines += [format_message(m) for m in new_messages]
        lines.append("")
    elif is_first_turn:
        lines.append("[대화 시작] 아직 아무도 발언하지 않았다. 당신이 첫 발언자다.")
        lines.append("")
    else:
        lines.append("[대화 업데이트] 당신의 지난 차례 이후 새 메시지가 없다.")
        lines.append("")
    if reminders:
        lines.append("[대기 중인 열람 요청 — respond_access_request로 허가 또는 거부하세요]")
        lines += [f"- {r}" for r in reminders]
        lines.append("")
    lines.append(f"이제 당신({agent_name})의 차례입니다.")
    return "\n".join(lines)
