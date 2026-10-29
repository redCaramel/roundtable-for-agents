"""대화 종료 제안 / 투표 (plan.md 1장 종료 조건, 4.4 propose_end·vote_end).

종료 정책
- ANY: 한 명이 제안하면 바로 끝난다.
- UNANIMOUS: 제안자는 찬성으로 치고, 나머지 전원이 찬성해야 끝난다. 한 명이라도 반대하면 부결.
- MODERATOR: 에이전트의 제안은 진행자에게 참고로만 전달되고, 진행자만 끝낼 수 있다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.core.models import EndPolicy


class EndVoteError(ValueError):
    """에이전트의 잘못된 제안·투표. 메시지는 그대로 에이전트에게 돌려준다."""


@dataclass
class EndProposal:
    id: int
    proposer: str
    reason: str
    votes: dict[str, bool] = field(default_factory=dict)


NoticeListener = Callable[[str], None]


class EndVote:
    def __init__(self, policy: EndPolicy, agent_names: Iterable[str]) -> None:
        self.policy = policy
        self.agent_names = list(agent_names)
        self.proposal: EndProposal | None = None
        # 종료가 확정되면 사유가 채워진다. 오케스트레이터가 이를 보고 대화를 끝낸다.
        self.ended_reason: str | None = None
        self._next_id = 1
        self._listeners: list[NoticeListener] = []

    def subscribe(self, listener: NoticeListener) -> None:
        self._listeners.append(listener)

    def _notify(self, text: str) -> None:
        for listener in self._listeners:
            listener(text)

    def _end(self, reason: str) -> None:
        self.ended_reason = reason
        self.proposal = None

    # ── 조회 ─────────────────────────────────────────
    def pending_voters(self) -> list[str]:
        """만장일치 투표에서 아직 의견을 내지 않은 참가자."""
        if self.proposal is None or self.policy != EndPolicy.UNANIMOUS:
            return []
        return [n for n in self.agent_names if n not in self.proposal.votes]

    def reminder_for(self, agent: str) -> str | None:
        """agent의 차례 업데이트에 넣을 투표 안내. 투표할 필요가 없으면 None."""
        p = self.proposal
        if p is None or agent not in self.pending_voters():
            return None
        return (
            f"{p.proposer}가 대화 종료를 제안했습니다 (제안 #{p.id}, 사유: {p.reason}). "
            "vote_end로 찬성(agree=true) 또는 반대(agree=false)를 표시하세요."
        )

    # ── 동작 ─────────────────────────────────────────
    def propose(self, agent: str, reason: str) -> str:
        if agent not in self.agent_names:
            raise EndVoteError(f"'{agent}'는 참가자가 아닙니다.")
        if self.ended_reason is not None:
            raise EndVoteError("이미 종료가 결정되었습니다.")
        reason = reason.strip() or "(사유 없음)"
        if self.proposal is not None:
            raise EndVoteError(
                f"이미 진행 중인 종료 제안 #{self.proposal.id}이 있습니다. "
                "vote_end로 의견을 표시하세요."
                if self.policy == EndPolicy.UNANIMOUS
                else f"이미 종료 제안 #{self.proposal.id}이 진행자에게 전달되어 있습니다."
            )

        if self.policy == EndPolicy.ANY:
            self._notify(f"{agent}가 대화 종료를 제안했습니다 (사유: {reason})")
            self._end(f"{agent}의 종료 제안")
            return "종료 제안이 받아들여져 이번 발언을 끝으로 대화가 끝납니다."

        self.proposal = EndProposal(self._next_id, agent, reason, {agent: True})
        self._next_id += 1
        if self.policy == EndPolicy.MODERATOR:
            self._notify(
                f"{agent}가 대화 종료를 제안했습니다 (제안 #{self.proposal.id}, 사유: {reason}). "
                "종료 여부는 진행자가 판단합니다."
            )
            return "종료 제안을 진행자에게 전달했습니다. 종료 여부는 진행자가 정합니다."
        self._notify(
            f"{agent}가 대화 종료를 제안했습니다 (제안 #{self.proposal.id}, 사유: {reason}). "
            "다른 참가자 전원이 찬성하면 대화가 끝납니다."
        )
        return f"종료 제안 #{self.proposal.id}을 올렸습니다. 다른 참가자 전원이 찬성하면 끝납니다."

    def vote(self, agent: str, agree: bool, reason: str) -> str:
        if self.policy != EndPolicy.UNANIMOUS:
            raise EndVoteError("이 대화에서는 종료 투표를 하지 않습니다.")
        p = self.proposal
        if p is None:
            raise EndVoteError(
                "진행 중인 종료 제안이 없습니다. 끝내고 싶다면 propose_end를 쓰세요."
            )
        if agent not in self.agent_names:
            raise EndVoteError(f"'{agent}'는 참가자가 아닙니다.")
        if agent in p.votes:
            raise EndVoteError(f"제안 #{p.id}에는 이미 의견을 냈습니다.")
        p.votes[agent] = agree
        suffix = f" — {reason.strip()}" if reason.strip() else ""
        if not agree:
            self._notify(f"{agent}가 종료 제안 #{p.id}에 반대해 부결되었습니다{suffix}")
            self.proposal = None
            return f"반대했습니다. 제안 #{p.id}은 부결되어 대화가 계속됩니다."
        self._notify(f"{agent}가 종료 제안 #{p.id}에 찬성했습니다{suffix}")
        if not self.pending_voters():
            self._notify(f"전원이 종료 제안 #{p.id}에 찬성했습니다")
            self._end(f"전원 동의 (제안 #{p.id})")
            return "찬성했습니다. 전원이 동의해 이번 발언을 끝으로 대화가 끝납니다."
        return f"찬성했습니다. 남은 참가자: {', '.join(self.pending_voters())}"

    def declare(self, reason: str) -> None:
        """진행자 선언으로 끝낸다 (정책이 MODERATOR일 때만 오케스트레이터가 부른다)."""
        reason = reason.strip() or "(사유 없음)"
        self._notify(f"진행자가 대화 종료를 선언했습니다 (사유: {reason})")
        self._end("진행자 선언")
