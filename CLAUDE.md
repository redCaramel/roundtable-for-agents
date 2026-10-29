# CLAUDE.md — roundtable-for-agents

Claude 에이전트 2명 이상이 사용자가 정한 주제(토론, 게임 규칙 등)로 서로 대화하는 Windows용 GUI 프로그램.

- 저장소: https://github.com/redCaramel/roundtable-for-agents
- 라이선스: MIT
- **상세 설계와 마일스톤은 [`plan.md`](plan.md)가 기준이다.** 설계를 바꾸는 작업이면 plan.md도 같이 고친다.

---

## 1. 기술 스택

- Python 3.11+
- GUI: PySide6 (+ `qasync`로 asyncio와 Qt 이벤트 루프 통합)
- 에이전트: Claude Agent SDK (`claude-agent-sdk`)
- 인증: **Claude 구독 계정 로그인으로 고정** (Claude Code 로그인). 사용량은 구독 한도에서 차감되며 API 키(종량제)는 쓰지 않는다
- 데이터 모델: pydantic
- 테스트: pytest (+ pytest-asyncio)

## 2. 폴더 구조 (요약)

```
app/
  main.py           # 진입점
  core/             # 대화 엔진 — GUI에 의존하지 않음
  gui/              # PySide6 화면 — 엔진 이벤트를 구독해 그리기만 함
  cli_debug.py      # GUI 없이 엔진만 돌려보는 콘솔 도구
tests/
conversations/      # 런타임 대화 데이터 (git 제외)
```

## 3. 개발 명령

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -r requirements.txt
claude                                              # 최초 1회: Claude Code 실행 후 /login 으로 구독 계정 로그인
python -m app.main                                  # GUI 실행
python -m app.cli_debug                             # 엔진만 콘솔 실행 (chat / smoke / list / resume, -h로 옵션)
pytest                                              # 테스트 (live 제외)
pytest -m live                                      # 실제 Claude 호출 테스트 (로그인 필요, 한도 차감)
ruff check . && ruff format .                       # 린트·포맷
```

> 위 명령은 M0~M1 진행 중에 실제로 확인하고 바뀌면 여기를 고친다.

## 4. 코드 규칙

- **`app/core`는 PySide6를 import하지 않는다.** 엔진 → GUI 통신은 이벤트(Qt Signal은 gui 쪽 어댑터에서 연결)로만 한다.
- **Claude Agent SDK 호출은 `app/core/agent_runner.py`(+ 진행자는 `moderator.py`)에만 둔다.** SDK가 바뀌어도 고칠 곳을 한정하기 위함.
- 샌드박스(`sandbox.py`) 규칙은 보안 경계다. 수정 시 `tests/test_sandbox.py`에 우회 시도 케이스를 추가하고 통과시킨다.
  - 에이전트에게 `Bash` 등 폴더 밖으로 나갈 수 있는 도구를 열지 않는다.
  - 판정은 `PreToolUse` 훅에서만 한다. `allowed_tools`에 도구를 넣지 않는다 (넣으면 판정 없이 자동 허용된다).
  - `strict_mcp_config=True`를 유지한다 (로그인 계정의 claude.ai 커넥터가 에이전트에 붙는 것 차단).
  - 모든 경로는 `Path.resolve()` 후 판정한다.
- 비동기 코드는 asyncio 기준. 블로킹 I/O를 이벤트 루프에서 직접 돌리지 않는다.
- 타입 힌트 필수, 포매터/린터는 `ruff` (도입 후 `ruff check` / `ruff format` 통과).
- 파일 인코딩은 UTF-8 고정 (한글 경로·폴더명 대응).
- UI 문구와 주석은 한국어, 식별자(변수·함수·클래스명)는 영어.

## 5. 보안·비밀 정보

- **API 키 방식으로 되돌리지 않는다.** `ANTHROPIC_API_KEY`·`ANTHROPIC_AUTH_TOKEN`이 환경에 있으면 종량제로 청구되므로, `agent_runner.ensure_subscription_auth()`가 앱 진입점에서 이를 제거하고 `build_options()`가 남은 키를 오류로 막는다. 이 보호 장치를 없애지 않는다.
- `.env`, 각종 토큰, `conversations/`(대화 로그·에이전트 개인 폴더)는 **절대 커밋하지 않는다.** `.gitignore`에 등록되어 있다.
- 테스트는 가짜 에이전트(mock)를 쓰고, 실제 Claude를 부르는 테스트는 별도 마커(`@pytest.mark.live`)로 분리한다 (구독 한도를 쓰므로 필요할 때만 실행).

---

## 6. Git 규칙

### 6.1 브랜치
- `main`: 항상 실행 가능한 상태 유지. **직접 커밋하지 않는다** (저장소 초기 세팅 커밋 제외).
- 작업은 기능 브랜치에서 하고 PR로 `main`에 병합한다.
- 브랜치 이름: `<type>/<짧은-설명>` (영어, kebab-case)
  - 마일스톤 작업: `feat/m1-core-engine`, `feat/m3-access-control`
  - 그 외: `fix/sandbox-symlink`, `docs/update-plan`, `refactor/agent-runner`, `chore/setup-ruff`
- 작업 시작 전 `main`을 최신으로 받고(`git pull`) 거기서 브랜치를 딴다.

### 6.2 커밋 메시지 — Conventional Commits, 영어
```
<type>(<scope>): <summary>

<body: 무엇을, 왜 바꿨는지 (선택)>
```
- `type`: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `style`, `perf`
- `scope`: `core`, `gui`, `sandbox`, `acl`, `moderator`, `storage`, `tools`, `prompts`, `deps`, `plan` 등 (생략 가능)
- `summary`: 명령형 현재 시제, 소문자 시작, 마침표 없음, 72자 이내
- 예시
  - `feat(core): add round-robin turn loop`
  - `fix(sandbox): block path traversal via symlinks`
  - `test(acl): cover revoke after grant`
  - `docs(plan): update milestone M2 criteria`
- 커밋 하나에는 논리적으로 하나의 변경만 담는다. 기능 + 무관한 리팩터링을 섞지 않는다.

### 6.3 Claude(에이전트)의 git 권한
| 작업 | 허락 필요? |
|---|---|
| 기능 브랜치 생성, 커밋 | **자유** — 의미 있는 작업 단위마다 알아서 커밋 |
| `git push` | **사용자에게 먼저 묻는다** |
| PR 생성 / `main` 병합 | **사용자에게 먼저 묻는다** |
| `push --force`, `reset --hard`, 히스토리 재작성, 브랜치 삭제 | **항상 묻는다** (원칙적으로 하지 않음) |

- 커밋 전에 테스트(`pytest`)와 린트를 돌리고, 실패하면 커밋하지 않거나 사유를 커밋 본문에 적는다.
- 커밋 전 `git status`/`git diff --staged`로 `.env`나 `conversations/`가 섞이지 않았는지 확인한다.
- 인증 만료 등으로 push가 실패하면 우회하지 말고 작업을 멈추고 사용자에게 알린다.

### 6.4 PR
- 제목은 커밋 형식과 동일 (`feat(core): ...`).
- 본문: 변경 요약, 관련 마일스톤(M0~M8), 테스트 방법/결과, 남은 과제.
- 병합 방식: **Squash and merge** (main 히스토리를 마일스톤·기능 단위로 깔끔하게 유지).
- 마일스톤 완료 시 plan.md의 해당 항목 상태를 함께 갱신한다.

---

## 7. 작업 방식

- 사용자에게 결정을 물을 때는 AskUserQuestion 도구를 쓰고, 추천 선택지를 첫 번째에 "(추천)"으로 둔다.
- 파일 삭제·덮어쓰기·이름 변경 전에는 사용자에게 확인한다.
- 마일스톤 단위로 진행하고, 각 마일스톤의 완료 기준(plan.md 10장)을 만족하는지 확인한 뒤 다음으로 넘어간다.
