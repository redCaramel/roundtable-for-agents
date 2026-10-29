"""대화 하나를 조립하거나 저장된 대화를 불러온다. 콘솔(cli_debug)과 GUI가 같이 쓴다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from app.core.access_control import AccessControl
from app.core.agent_runner import AgentRunner
from app.core.end_vote import EndVote
from app.core.events import EventSink
from app.core.models import Message, SessionConfig, TurnMode
from app.core.moderator import Moderator
from app.core.orchestrator import Orchestrator
from app.core.prompts import build_restore_context, build_system_prompt
from app.core.sandbox import SANDBOX_TOOLS, Sandbox
from app.core.storage import ACL_FILE, SessionSnapshot, SessionStore, agent_dir
from app.core.tools import ToolSpec, build_access_tools, build_end_tools

# 에이전트 이름 → 그 에이전트가 지금 읽을 수 있는 다른 폴더들 (M3에서 ACL이 채운다)
ReadableDirs = Callable[[str], Iterable[Path]]


def _none(_: str) -> Iterable[Path]:
    return ()


def acl_readable_dirs(acl: AccessControl, session_dir: Path) -> ReadableDirs:
    def dirs(reader: str) -> list[Path]:
        return [agent_dir(session_dir, owner) for owner in acl.readable_owners(reader)]

    return dirs


def create_agent_runners(
    config: SessionConfig,
    session_dir: Path,
    readable_dirs: ReadableDirs = _none,
    acl: AccessControl | None = None,
    current_turn: Callable[[], int] = lambda: 0,
    end_vote: EndVote | None = None,
    resume_sessions: dict[str, str | None] | None = None,
    transcript: Callable[[], Sequence[Message]] | None = None,
) -> list[AgentRunner]:
    """acl을 주면 열람 권한 도구를 붙이고, 읽기 허가 범위도 acl에서 가져온다.
    end_vote를 주면 종료 제안·투표 도구를 붙인다.
    resume_sessions를 주면 저장된 SDK 세션으로 이어하고, 실패하면 transcript()로 문맥을 복원한다."""
    resume_sessions = resume_sessions or {}
    if acl is not None:
        readable_dirs = acl_readable_dirs(acl, session_dir)
    runners = []
    for a in config.agents:
        own = agent_dir(session_dir, a.name)
        own.mkdir(parents=True, exist_ok=True)
        sandbox = Sandbox(
            own,
            readable_dirs=lambda name=a.name: readable_dirs(name),
            protected_root=session_dir,
        )
        runners.append(
            AgentRunner(
                name=a.name,
                model=a.model,
                system_prompt=build_system_prompt(config, a),
                cwd=own,
                tools=list(SANDBOX_TOOLS),
                sandbox=sandbox,
                custom_tools=_custom_tools(a.name, session_dir, acl, current_turn, end_vote),
                resume_session_id=resume_sessions.get(a.name),
                restore_context=_restorer(a.name, transcript),
            )
        )
    return runners


def _custom_tools(
    name: str,
    session_dir: Path,
    acl: AccessControl | None,
    current_turn: Callable[[], int],
    end_vote: EndVote | None,
) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    if acl is not None:
        tools += build_access_tools(
            acl, name, current_turn, folder_of=lambda owner: str(agent_dir(session_dir, owner))
        )
    if end_vote is not None:
        tools += build_end_tools(end_vote, name)
    return tools


def _restorer(
    name: str, transcript: Callable[[], Sequence[Message]] | None
) -> Callable[[], str] | None:
    if transcript is None:
        return None
    return lambda: build_restore_context(name, transcript())


def create_moderator(
    config: SessionConfig,
    session_dir: Path,
    resume_session_id: str | None = None,
    transcript: Callable[[], Sequence[Message]] | None = None,
) -> Moderator | None:
    """진행자 지목 모드일 때만 진행자를 만든다. 진행자는 도구 없이 판단만 한다."""
    if config.turn_mode != TurnMode.MODERATOR:
        return None
    cwd = session_dir / "moderator"
    cwd.mkdir(parents=True, exist_ok=True)
    return Moderator(config, cwd, resume_session_id, _restorer("진행자", transcript))


def build_orchestrator(
    config: SessionConfig,
    session_dir: Path,
    emit: EventSink | None = None,
    saved: tuple[SessionSnapshot, list[Message]] | None = None,
) -> Orchestrator:
    """대화 하나에 필요한 것(열람 권한, 종료 투표, 에이전트, 진행자, 자동 저장)을 모두 엮는다.

    saved를 주면 그 상태로 복원한다 (이어하기).
    """
    acl_path = session_dir / ACL_FILE
    acl = (
        AccessControl.load(config.agent_names, acl_path)
        if saved is not None
        else AccessControl(config.agent_names, acl_path)
    )
    end_vote = EndVote(config.end_policy, config.agent_names)
    holder: list[Orchestrator] = []

    def transcript() -> Sequence[Message]:
        return holder[0].transcript if holder else ()

    snap = saved[0] if saved is not None else None
    runners = create_agent_runners(
        config,
        session_dir,
        acl=acl,
        current_turn=lambda: holder[0].turn if holder else 0,
        end_vote=end_vote,
        resume_sessions=snap.agent_sessions if snap else None,
        transcript=transcript,
    )
    orch = Orchestrator(
        config,
        runners,
        emit or (lambda _: None),
        acl=acl,
        moderator=create_moderator(
            config, session_dir, snap.moderator_session if snap else None, transcript
        ),
        end_vote=end_vote,
        store=SessionStore(session_dir),
    )
    holder.append(orch)
    if saved is not None:
        orch.restore(*saved)
    else:
        orch.save()  # 바로 대화 목록에 보이도록 첫 스냅샷을 남긴다
    return orch


async def load_orchestrator(session_dir: Path, emit: EventSink | None = None) -> Orchestrator:
    """저장된 대화를 불러와 PAUSED 상태의 오케스트레이터를 만든다 (파일 읽기는 스레드에서)."""
    snap, messages = await asyncio.to_thread(SessionStore(session_dir).load)
    return build_orchestrator(snap.config, session_dir, emit, saved=(snap, messages))
