"""실제 API를 호출하는 스모크 테스트. `pytest -m live`로만 실행된다."""

import pytest
from dotenv import load_dotenv

from app.core.agent_runner import AgentRunner


@pytest.mark.live
async def test_single_agent_streams(tmp_path):
    load_dotenv()
    runner = AgentRunner(
        name="테스트",
        model="haiku",
        system_prompt="한 단어로만 답한다.",
        cwd=tmp_path / "agents" / "테스트",
    )
    chunks: list[str] = []
    try:
        result = await runner.run_turn("하늘은 무슨 색?", on_text=chunks.append)
    finally:
        await runner.disconnect()
    assert not result.is_error
    assert result.text
    assert chunks, "스트리밍 델타가 하나도 오지 않았다"
