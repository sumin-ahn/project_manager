# CLAUDE.md

> Claude Code 세션이 시작될 때 자동 로드되는 진입점. **새 세션이라면 먼저 이걸 읽고 → `.project_manager/wiki/board.md` 를 확인하라.**

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 새 세션 부트스트랩 (3 단계)

1. **상황 파악** — 다음 3 파일을 순서대로 본다:
   - [`.project_manager/wiki/board.md`](.project_manager/wiki/board.md) — 지금 무슨 ticket 잡을 수 있나?
   - [`.project_manager/wiki/status.md`](.project_manager/wiki/status.md) — 어디까지 됐나? (모듈 매트릭스 + 외부 의존성)
   - [`.project_manager/wiki/architecture.md`](.project_manager/wiki/architecture.md) — Layer / 파일 / 의존성 / 계약
   결정 근거는 [`.project_manager/wiki/decisions/`](.project_manager/wiki/decisions/).
2. **세션 이름 정하기 (옵션, 권장)** — 사용자가 식별할 수 있게 본인 이름을 정한다. 전달은 아래 3단계 `claim` 의 `--session` 인자로 한다 — 환경 무관하게 동작하며 VSCode extension 등 `export` 가 불가능한 환경에서도 OK. (CLI 환경이면 `export CLAUDE_SESSION_NAME=session-B` 도 가능. 둘 다 없으면 `<hostname>-<pid>` 자동 할당.) board.py 세션 식별 우선순위: `--session` 인자 > `$CLAUDE_SESSION_NAME` > `hostname-pid`.
3. **Ticket 잡기** — 외부 의존이 없고, 다른 세션이 이미 claim 하지 않은 것을 고른다:
   ```bash
   {{PY}} .project_manager/tools/board.py list --status open
   {{PY}} .project_manager/tools/board.py show T-NNNN
   {{PY}} .project_manager/tools/board.py claim T-NNNN --session session-B
   ```
   ticket 본문에 **목표 / 인터페이스 / 완료 조건 / 참고 링크** 가 들어 있다. 그것만 보고 작업이 가능해야 한다 — 부족하면 본문 자체를 보강하라.

## 작업이 끝나면

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass     # 또는
{{PY}} .project_manager/tools/board.py block T-NNNN --reason "..."
```

추가로:
- `.project_manager/wiki/status.md` 의 해당 모듈 행 갱신
- `.project_manager/wiki/log.md` 에 한 entry append
- 회귀 테스트 `{{TEST_CMD}}` 통과 확인
- **git commit** — 논리적 체크포인트(ticket 완료 등)에서 커밋. 커밋 메시지 말미에 `Co-Authored-By: Claude` 트레일러. 시크릿은 `.gitignore` 로 영구 제외.

## 작업 원칙 (반드시)

- **작은 단위 분할 → 단계별 테스트 검증.** 한 모듈 = 한 ticket = 한 단계.
- **테스트 없이는 구현이 끝난 게 아니다.** 회귀 통과가 완료 조건.
- **최소 변경.** ticket 이 요구한 것만. 무관한 코드 리포맷·기능 추가 금지.
- **약어보다 명시적 풀네임** — 의미를 정확히 담는 이름.
- **`.project_manager/` 는 숨김 디렉토리** — `ls -a` 또는 절대 경로로 접근. `.git`·`.claude` 와 같은 "프로젝트 인프라" 관례.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계를 적는다. 서브에이전트와
     모든 세션이 상속하는 절대 규칙. 예시 (finance 프로젝트):
       - 결정론 코어 vs LLM 분석층 경계 엄격 — 사이즈/한도/주문은 순수 코드,
         분석/시나리오는 LLM. 절대 섞지 않는다.
       - LLM 호출은 fail-soft — 예외를 raise 하지 않고 에러로 감싼다.
       - 외부 데이터는 sanitize 후에만 LLM 에 전달.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## 자주 쓰는 명령

```bash
# 전체 테스트 (현재 테스트 수는 .project_manager/wiki/status.md 가 단일 진실)
{{TEST_CMD}}

# 보드 조작
{{PY}} .project_manager/tools/board.py list
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --session <name>
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint     # 의존성·thin-ticket 일관성 검사

# PM workflow 도구 (PM 세션에서만)
{{PY}} .project_manager/tools/pm_bootstrap.py             # 세션 시작 부트스트랩 dump
{{PY}} .project_manager/tools/pm_handoff.py --session-num N --wave-summary "..."   # 세션 종료 핸드오프
{{PY}} .project_manager/tools/ticket_finish.py T-NNNN --section "<섹션>"           # ticket 완료 부기
```

PM workflow 는 `.claude/skills/pm-*` 슬래시 명령으로도 호출된다 (`/pm-bootstrap`, `/pm-handoff`, `/pm-wave-claim`, `/pm-wave-finish`, `/pm-dev-delegate`).

## 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py (숨김 디렉토리 — `ls -a`) |
| `.project_manager/wiki/` | 비-코드 산출물 (작업 / 결정 / 사양 / 상태 / 아키텍처 / raw 스냅샷) |
| `.claude/agents/` | developer · code-reviewer 서브에이전트 정의 |
| `.claude/skills/` | PM workflow slash command skill (pm-bootstrap·pm-handoff·pm-wave-claim·pm-wave-finish·pm-dev-delegate) |
<!-- TODO: 프로젝트의 실제 코드 디렉토리 행을 여기 추가한다. -->

## 막혔을 때

- 의존 ticket 이 아직 done 아니거나, 외부 키 없어 진행 불가 → `board.py block --reason "..."`.
- 잘못 claim 했다 → `board.py unclaim`.
- ticket 본문이 부족하다 → 먼저 본문 보강하고 계속.
- 모르는 결정이 필요하다 → ADR 작성 후 진행 ([`.project_manager/wiki/decisions/README.md`](.project_manager/wiki/decisions/README.md) 의 작성 절차).
