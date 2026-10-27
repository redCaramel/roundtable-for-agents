"""폴더 열람 요청 / 허가 / 철회 (plan.md 5.3).

허가는 읽기 전용이며 소유자가 철회할 때까지 유지된다. 상태 변화는 모두 채팅에
시스템 메시지로 공개된다(구독자에게 알림 문구를 넘긴다).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

DEFAULT_EXPIRE_TURNS = 10


class AccessError(ValueError):
    """에이전트의 잘못된 요청. 메시지는 그대로 에이전트에게 돌려준다."""


class RequestStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class AccessRequest(BaseModel):
    id: int
    requester: str
    target: str
    reason: str
    created_turn: int
    status: RequestStatus = RequestStatus.PENDING
    response_reason: str = ""


class Grant(BaseModel):
    reader: str
    owner: str


class _AclData(BaseModel):
    next_id: int = 1
    requests: list[AccessRequest] = []
    grants: list[Grant] = []


NoticeListener = Callable[[str], None]


class AccessControl:
    def __init__(
        self,
        agent_names: Iterable[str],
        path: Path | None = None,
        expire_turns: int = DEFAULT_EXPIRE_TURNS,
    ) -> None:
        self.agent_names = list(agent_names)
        self.path = path
        self.expire_turns = expire_turns
        self._data = _AclData()
        self._listeners: list[NoticeListener] = []

    # ── 알림 / 저장 ──────────────────────────────────
    def subscribe(self, listener: NoticeListener) -> None:
        self._listeners.append(listener)

    def _changed(self, notice: str) -> None:
        self.save()
        for listener in self._listeners:
            listener(notice)

    def save(self) -> None:
        if self.path is not None:
            self.path.write_text(self._data.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, agent_names: Iterable[str], path: Path, **kw: int) -> AccessControl:
        acl = cls(agent_names, path, **kw)
        if path.exists():
            acl._data = _AclData.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return acl

    # ── 조회 ─────────────────────────────────────────
    @property
    def requests(self) -> list[AccessRequest]:
        return list(self._data.requests)

    @property
    def grants(self) -> list[Grant]:
        return list(self._data.grants)

    def get(self, request_id: int) -> AccessRequest | None:
        return next((r for r in self._data.requests if r.id == request_id), None)

    def is_granted(self, reader: str, owner: str) -> bool:
        return any(g.reader == reader and g.owner == owner for g in self._data.grants)

    def readable_owners(self, reader: str) -> list[str]:
        """reader가 지금 읽을 수 있는 다른 에이전트 폴더의 소유자들."""
        return [g.owner for g in self._data.grants if g.reader == reader]

    def readers_of(self, owner: str) -> list[str]:
        return [g.reader for g in self._data.grants if g.owner == owner]

    def pending_for(self, target: str) -> list[AccessRequest]:
        return [
            r
            for r in self._data.requests
            if r.target == target and r.status == RequestStatus.PENDING
        ]

    def pending_from(self, requester: str) -> list[AccessRequest]:
        return [
            r
            for r in self._data.requests
            if r.requester == requester and r.status == RequestStatus.PENDING
        ]

    # ── 동작 ─────────────────────────────────────────
    def _check_agent(self, name: str) -> None:
        if name not in self.agent_names:
            raise AccessError(
                f"'{name}'라는 참가자는 없습니다. 참가자: {', '.join(self.agent_names)}"
            )

    def request(self, requester: str, target: str, reason: str, turn: int) -> AccessRequest:
        self._check_agent(requester)
        self._check_agent(target)
        if requester == target:
            raise AccessError("자기 폴더는 요청 없이 읽을 수 있습니다.")
        if self.is_granted(requester, target):
            raise AccessError(f"이미 {target}의 폴더를 읽을 수 있습니다.")
        if any(r.target == target for r in self.pending_from(requester)):
            raise AccessError(f"{target}에게 보낸 요청이 아직 대기 중입니다.")
        req = AccessRequest(
            id=self._data.next_id,
            requester=requester,
            target=target,
            reason=reason.strip() or "(사유 없음)",
            created_turn=turn,
        )
        self._data.next_id += 1
        self._data.requests.append(req)
        self._changed(
            f"{requester}가 {target}의 폴더 열람을 요청했습니다 "
            f"(요청 #{req.id}, 사유: {req.reason})"
        )
        return req

    def respond(self, responder: str, request_id: int, grant: bool, reason: str) -> AccessRequest:
        req = self.get(request_id)
        if req is None:
            raise AccessError(f"요청 #{request_id}를 찾을 수 없습니다.")
        if req.target != responder:
            raise AccessError(f"요청 #{request_id}는 당신에게 온 요청이 아닙니다.")
        if req.status != RequestStatus.PENDING:
            raise AccessError(f"요청 #{request_id}는 이미 처리되었습니다 ({req.status.value}).")
        req.status = RequestStatus.GRANTED if grant else RequestStatus.DENIED
        req.response_reason = reason.strip()
        if grant and not self.is_granted(req.requester, req.target):
            self._data.grants.append(Grant(reader=req.requester, owner=req.target))
        verb = "허가" if grant else "거부"
        suffix = f" — {req.response_reason}" if req.response_reason else ""
        self._changed(
            f"{responder}가 {req.requester}의 폴더 열람을 {verb}했습니다 (요청 #{req.id}){suffix}"
        )
        return req

    def revoke(self, owner: str, reader: str) -> None:
        self._check_agent(reader)
        if not self.is_granted(reader, owner):
            raise AccessError(f"{reader}에게 준 열람 허가가 없습니다.")
        self._data.grants = [
            g for g in self._data.grants if not (g.reader == reader and g.owner == owner)
        ]
        self._changed(f"{owner}가 {reader}의 폴더 열람 권한을 철회했습니다")

    def expire(self, current_turn: int) -> list[AccessRequest]:
        """응답 없이 expire_turns 이상 지난 요청을 만료시킨다."""
        expired = []
        for r in self._data.requests:
            if (
                r.status == RequestStatus.PENDING
                and current_turn - r.created_turn >= self.expire_turns
            ):
                r.status = RequestStatus.EXPIRED
                expired.append(r)
        for r in expired:
            self._changed(
                f"요청 #{r.id}({r.requester} → {r.target})가 "
                f"{self.expire_turns}턴 동안 응답이 없어 만료되었습니다"
            )
        return expired
