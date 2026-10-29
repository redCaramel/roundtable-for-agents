"""시스템 프롬프트와 턴 업데이트 템플릿 (plan.md 4.1, 4.3)."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.models import AgentConfig, EndPolicy, Message, SessionConfig, TurnMode

_END_POLICY_TEXT = {
    EndPolicy.UNANIMOUS: (
        "- 대화를 끝낼 때가 되었다고 생각하면 propose_end로 제안한다. "
        "다른 참가자가 제안하면 vote_end로 찬반을 표시한다. 전원이 찬성하면 끝난다."
    ),
    EndPolicy.ANY: (
        "- 대화를 끝낼 때가 되었다고 생각하면 propose_end로 제안한다. 제안하면 바로 끝난다."
    ),
    EndPolicy.MODERATOR: (
        "- 끝낼 때가 되었다고 생각하면 propose_end로 제안할 수 있다. 종료 여부는 진행자가 정한다."
    ),
}

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
        "",
        "# 대화 종료",
        _END_POLICY_TEXT[config.end_policy],
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
    was_interrupted: bool = False,
    end_vote_reminder: str | None = None,
) -> str:
    lines: list[str] = []
    if was_interrupted:
        lines.append("[알림] 당신의 직전 발언은 중단되어 다른 참가자에게 전달되지 않았다.")
        lines.append("")
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
    if end_vote_reminder:
        lines.append(f"[종료 투표] {end_vote_reminder}")
        lines.append("")
    lines.append(f"이제 당신({agent_name})의 차례입니다.")
    return "\n".join(lines)


# ── 진행자 (plan.md 4.5) ─────────────────────────────
def build_moderator_system_prompt(config: SessionConfig) -> str:
    can_end = config.end_policy == EndPolicy.MODERATOR
    parts = [
        "# 역할",
        "당신은 여러 참가자가 나누는 대화의 진행자다. 대화에는 참여하지 않고, "
        "매번 다음에 말할 참가자 한 명만 고른다.",
        f"참가자: {', '.join(config.agent_names)}",
        "",
        "# 주제 / 규칙",
        config.topic.strip(),
        "",
        "# 고르는 기준",
        "- 대화 흐름상 지금 말해야 할 사람 (질문을 받은 사람, 반론할 차례인 사람 등)",
        "- 대기 중인 폴더 열람 요청이 있으면 그 요청을 받은 사람을 우선한다.",
        "- 오래 말하지 않은 참가자를 배려한다. 한 사람만 계속 고르지 않는다.",
        "- '[사용자]' 메시지는 대화를 지켜보는 사용자의 개입이다. 그 요청을 반영한다.",
    ]
    if can_end:
        parts += [
            "",
            "# 종료 판단",
            "- 주제의 목적을 이뤘거나 규칙상 끝날 때(예: 게임의 승패가 정해짐)가 되면 "
            "end를 true로 한다.",
            "- 참가자가 종료를 제안하면 참고하되, 최종 판단은 당신이 한다.",
        ]
    parts += [
        "",
        "# 답변 형식",
        "다른 말 없이 JSON 한 줄로만 답한다.",
        '{"next": "<참가자 이름>", "reason": "<한 문장 이유>", "end": '
        + ("<true 또는 false>" if can_end else "false")
        + "}",
    ]
    return "\n".join(parts)


def build_moderator_update(
    new_messages: Sequence[Message],
    status_lines: Sequence[str],
    notes: Sequence[str] = (),
) -> str:
    """진행자에게 매 턴 보내는 입력: 지난 판단 이후 메시지 + 참가자 현황 + 참고 사항."""
    lines: list[str] = []
    if new_messages:
        lines.append("[대화 업데이트]")
        lines += [format_message(m) for m in new_messages]
    else:
        lines.append("[대화 업데이트] 새 메시지가 없다.")
    lines.append("")
    lines.append("[참가자 현황]")
    lines += [f"- {s}" for s in status_lines]
    if notes:
        lines.append("")
        lines.append("[참고]")
        lines += [f"- {n}" for n in notes]
    lines.append("")
    lines.append("다음 발언자를 JSON으로 답하라.")
    return "\n".join(lines)


# ── 이어하기 실패 시 문맥 복원 (plan.md 8장) ─────────
RESTORE_MAX_CHARS = 20_000


def build_restore_context(name: str, messages: Sequence[Message]) -> str:
    """저장된 SDK 세션을 찾지 못해 새 세션을 열 때, 첫 메시지 앞에 붙이는 지난 대화 기록.

    길면 최근 메시지부터 RESTORE_MAX_CHARS까지만 넣는다.
    """
    lines: list[str] = []
    used = 0
    for m in reversed(messages):
        line = format_message(m)
        if used + len(line) > RESTORE_MAX_CHARS:
            lines.append("(…더 오래된 대화는 생략…)")
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    header = (
        f"[이전 대화 복원] 이전 세션을 불러오지 못해 지금까지의 대화 기록을 다시 전달한다. "
        f"'{name}:'로 시작하는 줄은 당신이 했던 발언이다."
    )
    if not messages:
        return ""
    return "\n".join([header, *lines, "[복원 끝]"])
