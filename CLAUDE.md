# CLAUDE.md

> Claude Code 세션이 시작될 때 자동 로드되는 진입점. **새 세션이라면 먼저 이걸 읽고 → `board.py list` 로 보드를 확인하라.**

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 새 세션 부트스트랩 (3 단계)

1. **상황 파악** — 다음을 순서대로 본다:
   - 보드 — `{{PY}} .project_manager/tools/board.py list` (지금 무슨 ticket 잡을 수 있나). `board.md` 는 이걸 렌더한 파생 대시보드라 git 에 없을 수 있다 — 파일로 보려면 `board.py refresh`.
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

## 멀티-PM clone (동시 다중 PM 프로젝트)

여러 사람이 각자 clone 해 영역을 나눠 PM 하는 프로젝트라면 **clone 당 1회** 등록:

```bash
{{PY}} .project_manager/tools/board.py init --prefix PAY --area "결제" --owner alice
```

- `areas.md`(공유 레지스트리) prefix 등록 + `local.conf`(per-clone·git-ignored) 생성 + `pm_state.md` 로컬 생성.
- 이후 `board.py new` 는 `T-PAY-NNN` 으로 발행 (영역별 네임스페이스 → 동시 발행 ID 충돌 없음).
- **3계층:** 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf).
- **솔로(개인/toy):** `board.py init` (prefix 없이) → 솔로 setup(pm_state·pre-push 회귀 훅·legacy `T-NNNN`, areas.md 안 만듦). 안 해도 `board.py new` 는 동작(graceful).

## 작업이 끝나면

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass     # 또는
{{PY}} .project_manager/tools/board.py block T-NNNN --reason "..."
```

추가로:
- `.project_manager/wiki/status.md` 의 해당 모듈 행 갱신
- `.project_manager/wiki/log/current.md` 에 한 entry append
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
     모든 세션이 상속하는 절대 규칙. 예시 (도메인 무관):
       - 핵심 결정 로직(순수·결정론) ↔ 분석/생성 계층(LLM 등 비결정) 경계 엄격 — 섞지 않는다.
       - LLM·외부 호출 래퍼는 fail-soft — 예외를 raise 하지 않고 에러로 감싼다.
       - 외부 입력은 sanitize 후에만 핵심 로직/LLM 에 전달.
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

# 프레임워크 갱신 (메인테이너·업그레이드당 1회 — 절차는 pm_playbook §"프레임워크 갱신"):
{{PY}} .project_manager/tools/pm_update.py --from <upstream-checkout> --dry-run   # 엔진만 당겨옴 → --dry-run 빼고 적용 → 검증 → 커밋·push

# PM workflow 도구(pm_bootstrap·pm_handoff·ticket_finish)는 PM 세션 전용 — 용법·플래그·호출(/pm-*)의
# 단일 진실은 pm_role.md §"skill 카탈로그" + 각 .claude/skills/pm-*/SKILL.md. 여기에 재나열하지 않는다.
# log 관리 도구만 여기 (전용 skill 없음):
{{PY}} .project_manager/tools/pm_log.py tail                       # 마지막 entry 만 (의미단위 읽기)
{{PY}} .project_manager/tools/pm_log.py archive --before YYYY-MM-DD  # 그 이전 entry 를 log/archive/ 봉인
{{PY}} .project_manager/tools/pm_log.py migrate                    # 기존 log.md → archive/0000-legacy (도입 1회)
```

## 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py (숨김 디렉토리 — `ls -a`) |
| `.project_manager/wiki/` | 비-코드 산출물 (작업 / 결정 / 사양 / 상태 / 아키텍처 / pm_role·pm_state·pm_playbook / log/ / raw 스냅샷) |
| `.claude/agents/` | architect(Opus) · developer · code-reviewer 서브에이전트 정의 |
| `.claude/skills/` | PM workflow slash command skill (목록·역할·backbone → pm_role.md §"skill 카탈로그") |
<!-- TODO: 프로젝트의 실제 코드 디렉토리 행을 여기 추가한다. -->

## 막혔을 때

- 의존 ticket 이 아직 done 아니거나, 외부 키 없어 진행 불가 → `board.py block --reason "..."`.
- 잘못 claim 했다 → `board.py unclaim`.
- ticket 본문이 부족하다 → 먼저 본문 보강하고 계속.
- 모르는 결정이 필요하다 → ADR 작성 후 진행 ([`.project_manager/wiki/decisions/README.md`](.project_manager/wiki/decisions/README.md) 의 작성 절차).
