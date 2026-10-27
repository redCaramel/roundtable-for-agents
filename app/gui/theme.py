"""기본 색 팔레트와 아바타 그리기."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap

# 에이전트 기본 색 (서로 잘 구분되고, 밝은/어두운 배경 모두에서 읽히는 중간 채도)
AGENT_COLORS: list[str] = ["#4a90d9", "#e07b39", "#5aa469", "#b05fc2", "#d4a72c", "#d0565c"]
DEFAULT_NAMES: list[str] = ["철수", "영희", "민수", "지은", "현우", "수아"]
MODELS: list[tuple[str, str]] = [("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku")]

USER_COLOR = "#8a8f98"
AVATAR_SIZE = 36


def tint(color: str, alpha: int) -> str:
    """말풍선 배경용 반투명 색 (rgba 문자열)."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def avatar_pixmap(
    name: str, color: str, image_path: str | None, size: int = AVATAR_SIZE
) -> QPixmap:
    """이미지가 있으면 원형으로 잘라 쓰고, 없으면 이름 첫 글자를 색 원 위에 그린다."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, size, size))
    p.setClipPath(clip)

    src = QPixmap(image_path) if image_path else QPixmap()
    if not src.isNull():
        scaled = src.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    else:
        p.fillRect(0, 0, size, size, QColor(color))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.45))
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, name[:1])
    p.end()
    return pm
