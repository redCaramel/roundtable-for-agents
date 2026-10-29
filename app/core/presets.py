"""대화 설정 프리셋 저장 / 불러오기 (plan.md 6.4). 파일 하나에 프리셋 하나 (UTF-8 JSON)."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from app.core.models import SessionConfig
from app.core.storage import safe_dirname

log = logging.getLogger(__name__)


class Preset(BaseModel):
    name: str
    config: SessionConfig


def preset_path(root: Path, name: str) -> Path:
    return root / f"{safe_dirname(name, 'preset')}.json"


def save_preset(root: Path, name: str, config: SessionConfig) -> tuple[Path, bool]:
    """저장한 경로와, 같은 이름이 있어 덮어썼는지 여부를 돌려준다."""
    name = name.strip()
    if not name:
        raise ValueError("프리셋 이름을 입력하세요.")
    root.mkdir(parents=True, exist_ok=True)
    path = preset_path(root, name)
    existed = path.exists()
    path.write_text(Preset(name=name, config=config).model_dump_json(indent=2), encoding="utf-8")
    return path, existed


def list_presets(root: Path) -> list[Preset]:
    """이름순 목록. 읽을 수 없는 파일은 건너뛴다."""
    presets: list[Preset] = []
    if not root.is_dir():
        return presets
    for path in sorted(root.glob("*.json")):
        try:
            presets.append(Preset.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            log.warning("프리셋을 읽을 수 없음: %s", path)
    presets.sort(key=lambda p: p.name)
    return presets
