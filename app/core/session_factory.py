"""설정으로부터 에이전트 러너들을 만든다. 콘솔(cli_debug)과 GUI가 같이 쓴다."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from app.core.access_control import AccessControl
from app.core.agent_runner import AgentRunner
from app.core.end_vote import EndVote
from app.core.models import SessionConfig, TurnMode
from app.core.moderator import Moderator
from app.core.prompts import build_system_prompt
from app.core.sandbox import SANDBOX_TOOLS, Sandbox
from app.core.storage import agent_dir
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
) -> list[AgentRunner]:
    """acl을 주면 열람 권한 도구를 붙이고, 읽기 허가 범위도 acl에서 가져온다.
    end_vote를 주면 종료 제안·투표 도구를 붙인다."""
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


def create_moderator(config: SessionConfig, session_dir: Path) -> Moderator | None:
    """진행자 지목 모드일 때만 진행자를 만든다. 진행자는 도구 없이 판단만 한다."""
    if config.turn_mode != TurnMode.MODERATOR:
        return None
    cwd = session_dir / "moderator"
    cwd.mkdir(parents=True, exist_ok=True)
    return Moderator(config, cwd)
