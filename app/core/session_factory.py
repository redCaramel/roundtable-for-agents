"""설정으로부터 에이전트 러너들을 만든다. 콘솔(cli_debug)과 GUI가 같이 쓴다."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from app.core.agent_runner import AgentRunner
from app.core.models import SessionConfig
from app.core.prompts import build_system_prompt
from app.core.sandbox import SANDBOX_TOOLS, Sandbox
from app.core.storage import agent_dir

# 에이전트 이름 → 그 에이전트가 지금 읽을 수 있는 다른 폴더들 (M3에서 ACL이 채운다)
ReadableDirs = Callable[[str], Iterable[Path]]


def _none(_: str) -> Iterable[Path]:
    return ()


def create_agent_runners(
    config: SessionConfig, session_dir: Path, readable_dirs: ReadableDirs = _none
) -> list[AgentRunner]:
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
            )
        )
    return runners
