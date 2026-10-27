"""GUI 진입점: python -m app.main"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import qasync
from PySide6.QtWidgets import QApplication

from app.core.agent_runner import ensure_subscription_auth
from app.gui.main_window import MainWindow

ROOT = Path(__file__).resolve().parent.parent
CONVERSATIONS = ROOT / "conversations"


def main() -> int:
    # 구독 로그인만 쓴다 — 가장 먼저 API 키 환경변수를 이 프로세스에서 지운다.
    ensure_subscription_auth()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Roundtable for Agents")
    app.setStyle("Fusion")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    closed = asyncio.Event()
    app.aboutToQuit.connect(closed.set)

    window = MainWindow(CONVERSATIONS)
    window.show()
    window.open_setup()
    with loop:
        loop.run_until_complete(closed.wait())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
