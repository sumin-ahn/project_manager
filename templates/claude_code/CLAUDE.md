# CLAUDE.md

> Claude Code 세션 시작 시 자동 로드되는 진입점.

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 새 세션 부트스트랩

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `/pm-bootstrap` dump 한 번뿐이다.

1. **이 문서** — 자동 로드된 프로젝트 규칙·형상.
2. **현재 정체성의 `pm_state`** — `.project_manager/.local/tasks/<task>/pm_state.md`다(git-ignored).
   신규 task는 bootstrap 진입 전 파일이 없어도 정상이다.
3. **`/pm-bootstrap` dump 한 번** — board·git·차수·직전 handoff 본문·남은 작업을 한꺼번에
   surface한다. Python backbone은 `{{PY}} .project_manager/tools/pm_bootstrap.py`다.
<!-- pm-bootstrap-preread:end -->

정식 계약은 [`.project_manager/wiki/pm_role.md`](.project_manager/wiki/pm_role.md) §부트스트랩이
단일 진실이다. `architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는
시작 시 통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다.

**현재 진실:** [`.project_manager/wiki/architecture.md`](.project_manager/wiki/architecture.md)는
현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 의도·실측과 충돌하면 기준으로 따른다. 바뀐 것은
읽는 시점뿐이다.

### 부트스트랩 후 ticket 잡기

세션 정체성은 task 이름 하나다. 귀속 쓰기(new·claim 등)는 `--task <task>` 로 하고, 없으면
fail-loud한다. 조회·소유 대조의 세션 식별 우선순위는 `--task` > `$PM_SESSION_NAME` > 활성 슬롯
lease가 정확히 1개면 그 세션(단일-lease 유도) > 미해소다. lease 행이 0개인 홈은 아직 작업 폴더가
없는 것이며 `pm-config worktree add <repo> --user-ack <repo>` 로 만든 뒤
`pm-config alloc <repo> --task <task>` 로 task 에 빌린다.

다중 작업공간에서는 claim 앵커·regression run·livegate record·new/cluster new 코드 트리·rounds resolve에만 `--task <task> --repo <repo> --slot <N>`을 함께 준다(`--repo`/`--slot` 은 `--task` 와 함께일 때만 받는 위치 인자다). 자기 task의 소유 슬롯을 명시하고 HEAD·브랜치·test_cmd를 결속한다. bootstrap·handoff·init·reid는 task 단독 입력이고, finish 의 `--repo`/`--slot` 은 복구 문(`--reconcile-integrated`) 전용이며, finish 내부 확인은 묶음 브랜치로 트리를 찾는다.

외부 의존이 없고 다른 세션이 claim하지 않은 ticket을 고른다:

```bash
{{PY}} .project_manager/tools/board.py list --status open
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --task <task>   # task 의 폴더가 2개 이상이면 --repo <repo> --slot <N> 추가
```

ticket 본문의 **목표 / 인터페이스 / 완료 조건 / 참고 링크**만으로 작업 가능해야 한다. 부족하면
본문부터 보강한다.

## 멀티-PM clone (동시 다중 PM 프로젝트)

여러 clone 에서 영역을 나눠 PM 하면 **clone 당 1회**:

```bash
{{PY}} .project_manager/tools/board.py init --prefix pay --area "결제" --owner alice
```

- `areas.md`(공유 레지스트리) prefix 등록 + `local.conf`(per-clone·git-ignored) 생성 + `pm_state.md` 로컬 생성.
- 이후 `board.py new` 는 영역별 네임스페이스 `T-pay-NNN` 으로 발행한다.
- **3계층:** 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf).
- **repo 1개:** `pm-config init`(prefix 없이) → areas 행·pre-push 회귀 훅·`T-NNNN` setup. 그 다음 `pm-config worktree add <repo> --user-ack <repo>` 로 작업 폴더를 만들고 `pm-config alloc <repo> --task <task>` 로 task 에 빌린다 — `board.py new`·`claim` 은 `--task` 와 그 task 폴더가 있어야 한다.

## 작업이 끝나면

```bash
{{PY}} .project_manager/tools/ticket_finish.py --cluster C-<ticket-group> [--task <task>]     # 또는
{{PY}} .project_manager/tools/board.py block T-NNNN --reason "..."
```

추가 작업:
- `.project_manager/wiki/status.md` 해당 모듈 행 갱신
- `.project_manager/wiki/log/current.md` 에 entry append
- 회귀 테스트 `{{TEST_CMD}}` 통과 확인
- **git commit** — 논리적 체크포인트에서 **커밋 경로를 명시**:
  ```bash
  git add <이번에 새로 만든 파일 경로들>          # 미추적 경로는 add 선행 필수 (아래 주의)
  git commit -m "T-NNNN — <요약>" -- \
    <ticket touches 의 실경로들> \
    .project_manager/wiki/status.md .project_manager/wiki/log/current.md \
    .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
    .project_manager/wiki/tickets/done/T-NNNN-<slug>.md
  ```
  pathspec 없는 bare commit 은 남이 stage 한 무관한 변경도 싣는다. ⚠️ **미추적(신규) 파일은 `git add` 선행** — pathspec 에 바로 주면 `error: pathspec '…' did not match any file(s) known to git` 으로 전체 commit 이 rc=1. ⚠️ ticket 이 `claimed/`→`done/` 으로 이동했으면 **옛/새 경로를 함께** 줘야 이동이 커밋된다. 메시지 말미에 `Co-Authored-By: Claude` 트레일러. 시크릿은 `.gitignore` 로 영구 제외.

> **참조 규약**: status·log·ticket·ADR 의 ADR/ticket/idea 참조는 ID-wikilink(`[[ADR-NNNN]]`·`[[T-NNNN]]`·`[[idea-NNNN]]`)만 사용. 생파일명·슬러그 금지(`board.py lint --gate` 강제). 단일 진실은 pm_playbook §참조 규약.

## 작업 원칙 (반드시)

- **작은 단위 분할 → 단계별 테스트 검증.** 한 모듈 = 한 ticket = 한 단계.
- **테스트 없이는 구현이 끝난 게 아니다.** 회귀 통과가 완료 조건.
- **최소 변경.** ticket 요구만 수행하고 무관한 리포맷·기능 추가 금지.
- **약어보다 명시적 풀네임**을 사용한다.
- **`.project_manager/` 는 숨김 디렉토리** — `ls -a` 또는 절대 경로로 접근.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계를 적는다. 서브에이전트와
     모든 세션이 상속하는 절대 규칙. 예시 (도메인 무관):
       - 핵심 결정 로직(순수·결정론) ↔ 분석/생성 계층(LLM 등 비결정) 경계 엄격 — 섞지 않는다.
       - LLM·외부 API 호출 래퍼는 fail-soft — 예외를 raise 하지 않고 에러로 감싼다.
       - 외부 입력은 sanitize 후에만 핵심 로직/LLM 에 전달.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## Windows / 인코딩 (CP949 캐비엇)

- 엔진 wiki·ticket 은 UTF-8. 파일 IO 는 `encoding="utf-8"`, 콘솔은 `sys.stdout/stderr.reconfigure(encoding="utf-8")` 로 처리하므로 **Windows/CP949·PowerShell 에서도 env prefix 없이** board·log 한글이 동작한다. 외부 파이프·서드파티 도구·구버전 콘솔에서 필요하면 셸별로 PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1` 을 붙인다.
- **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError) — `cd X && cmd` 금지. 도구 workdir 파라미터나 명령 분리로 실행한다. Windows 루트 facade 는 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요), `./pm-*.sh` 는 bash 용.
- `{{PY}}` 는 `board.py init` 이 PATH 탐지로 채운다(Windows=`python`·POSIX=`python3`). venv 이면 PM workflow 도구가 `venv/Scripts/python.exe`(Windows)·`venv/bin/python`(POSIX)을 자동 선택하고, 없으면 현재 인터프리터로 폴백한다.

## 자주 쓰는 명령

```bash
# 전체 테스트 (테스트 수 단일 진실 = pytest 실측 · status.md 는 숫자 미저장·judgment-only·ADR-0023)
{{TEST_CMD}}

# 보드 조작
{{PY}} .project_manager/tools/board.py list
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --task <task>
{{PY}} .project_manager/tools/ticket_finish.py --cluster C-<ticket-group> [--task <task>]
{{PY}} .project_manager/tools/board.py new "title" --task <task> --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint     # 의존성·thin-ticket 일관성 검사

# 프레임워크 갱신 (메인테이너·업그레이드당 1회 — 절차는 pm_playbook §"프레임워크 갱신"):
{{PY}} .project_manager/tools/pm_update.py --from <upstream-checkout> --dry-run   # 엔진만 당겨옴 → --dry-run 빼고 적용 → 검증 → 커밋·push

# PM workflow 도구(pm_bootstrap·pm_handoff·ticket_finish)는 PM 세션 전용 — 용법·플래그·호출(/pm-*)의
# 단일 진실은 pm_role.md §"skill 카탈로그" + 각 .claude/skills/pm-*/SKILL.md. 여기에 재나열하지 않는다.
# log 관리 도구만 여기 (전용 skill 없음):
{{PY}} .project_manager/tools/pm_log.py tail                       # 마지막 entry 만 (의미단위 읽기)
{{PY}} .project_manager/tools/pm_log.py archive --before YYYY-MM-DD  # 그 이전 entry 를 log/archive/ 봉인
{{PY}} .project_manager/tools/pm_log.py migrate                    # 기존 log.md → archive/0000-legacy (도입 1회)

# domain 지식 레이어 (살아있는 프로젝트 지식 — 명령은 아래·살아있는 루프 전체는 프레임워크 가이드 · ADR-0018)
{{PY}} .project_manager/tools/domain.py list                       # 페이지 카탈로그 (type·covers·stale)
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN   # ticket touches∩covers 페이지 소환
{{PY}} .project_manager/tools/domain.py capture --tickets T-NNNN   # 갱신 reminder (채록)
{{PY}} .project_manager/tools/domain.py lint                       # freshness — stale 페이지 검사
```

> **컨텍스트 압축 기준:** 하네스별 희망 기준 `harness.<name>.ctx_window_tokens`(기본 200000토큰)와 공통 비율 3개로 설정한다. 설정한 희망 기준을 보존하며, 실제 적용 기준은 독립 근거로 확인한 모델의 가용 한도 이하로 제한합니다. 모델 가용 한도 C는 적용(`/pm-update`·`pm-config apply`·관리자 저장) 때 한 번 조회해 압축 한도 기록에 고정하고, 실제 적용 기준 E=min(W,C)를 체크포인트·경고·압축·relay가 함께 사용한다. 비율은 E 대비 사용률이며 `ctx.checkpoint_at_pct`(체크포인트·기본70%)·`ctx.precompact_at_pct`(압축 임박 경고·기본80%)·`ctx.compact_at_pct`(압축·기본90%)다. C를 조회하지 못하면 적용 전에 거부하고 설정 파일·기록을 바꾸지 않는다. 입력은 local.conf에서 설정한다. 실행 중에는 기록만 읽으며, 세션 모델이 기록 모델과 다르면 경고하고 기록이 없거나 local.conf 입력과 다르면 오류로 알린다. 같은 모델의 한도가 바뀌거나 경고·오류가 나면 `pm-config apply`를 다시 실행한다.

## 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py (숨김 — `ls -a`) |
| `.project_manager/wiki/` | 비-코드 산출물(작업·결정·사양·상태·domain·pm_role·pm_state·pm_playbook·log·raw) |
| `.claude/agents/` | researcher · architect(Opus) · developer · code-reviewer 서브에이전트 정의 |
| `.claude/skills/` | PM workflow slash command skill(단일 진실: pm_role.md §skill 카탈로그) |
<!-- TODO: 프로젝트의 실제 코드 디렉토리 행을 여기 추가한다. -->

## 막혔을 때

- 의존 ticket 미완 또는 외부 키 없음 → `board.py block --reason "..."`.
- 잘못 claim → `board.py unclaim`.
- ticket 본문 부족 → 본문부터 보강.
- 모르는 결정 필요 → [`.project_manager/wiki/decisions/README.md`](.project_manager/wiki/decisions/README.md) 절차로 ADR 작성 후 진행.
