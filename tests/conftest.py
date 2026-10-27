import os

# GUI 테스트는 화면 없이 돌린다 (PySide6 import 전에 설정해야 한다).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
