"""오류를 사용자가 할 일이 보이는 안내문으로 바꾼다.

plan.md 11장의 위험 대응: 로그인 만료·사용 한도·네트워크 오류는 자동 재시도하지 않고
일시정지한 뒤 원인과 할 일을 보여준다.
"""

from __future__ import annotations

_MAX_DETAIL = 160

# (찾을 단어들, 안내문) — 위에서부터 먼저 맞는 것을 쓴다
_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("clinotfound", "claude code not found", "cli not found"),
        "Claude Code 실행 파일을 찾을 수 없습니다. `pip install -r requirements.txt`로 "
        "claude-agent-sdk를 다시 설치하세요.",
    ),
    (
        (
            "not logged in",
            "please run /login",
            "invalid api key",
            "authentication",
            "unauthorized",
            "oauth",
            "401",
            "credential",
        ),
        "Claude 로그인이 필요하거나 만료되었습니다. 터미널에서 `claude`를 실행한 뒤 "
        "/login으로 구독 계정에 로그인하고 ▶ 재개를 누르세요.",
    ),
    (
        ("usage limit", "rate limit", "rate_limit", "limit reached", "429", "quota"),
        "구독 사용 한도에 도달했습니다. 한도가 풀린 뒤 ▶ 재개를 누르세요.",
    ),
    (
        ("overloaded", "529", "503", "service unavailable"),
        "Claude 서버가 혼잡합니다. 잠시 후 ▶ 재개를 누르세요.",
    ),
    (
        (
            "connection",
            "network",
            "네트워크",
            "timed out",
            "timeout",
            "econn",
            "getaddrinfo",
            "ssl",
        ),
        "네트워크 연결에 문제가 있습니다. 인터넷 연결을 확인한 뒤 ▶ 재개를 누르세요.",
    ),
]


def friendly_error(error: BaseException | str) -> str:
    """오류 종류별 안내문. 알 수 없는 오류는 원문을 짧게 보여준다."""
    raw = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    lowered = raw.lower()
    for keywords, hint in _RULES:
        if any(k in lowered for k in keywords):
            return hint
    detail = raw.strip() or "알 수 없는 오류"
    if len(detail) > _MAX_DETAIL:
        detail = detail[: _MAX_DETAIL - 1] + "…"
    return f"{detail} — 원인을 해결한 뒤 ▶ 재개를 누르세요."
