# Tickets — 멀티 세션 개발 작업 보드

여러 Claude Code 세션이 **충돌 없이 병렬로** 작업할 수 있게 하는 가벼운 ticket 시스템.

## 핵심 약속

- **디렉토리 = 상태.** 한 ticket 파일은 `open/`, `claimed/`, `blocked/`, `done/` 중 정확히 한 곳에 있다.
- **`mv` 가 atomic 한 lock.** POSIX rename(2) 은 동일 파일시스템에서 atomic 이라, 두 세션이 동시에 claim 시도해도 한 쪽만 성공한다 (`.project_manager/tools/board.py claim` 이 wrapping).
- **`board.py list` 가 라이브 상태.** tickets/ 디렉토리가 단일 진실이고 `board.py list` 는 항상 그걸 직접 읽는다. `board.md` 는 `board.py` 명령마다 갱신되는 로컬 파생 대시보드(git-untracked) — clone 간 공유 안 됨.
- **frontmatter 가 진실.** 본문 변경은 자유, 헤더는 `board.py` 가 관리.

## 워크플로

### 새 세션 시작 시
```bash
# 1) 현재 상황 확인 (라이브 — board.md 파일 없어도 동작)
{{PY}} .project_manager/tools/board.py list

# 2) 세션 이름 정하기 (claim 의 --session 인자로 전달 — 없으면 hostname-pid 자동)
```

### Ticket 잡고 작업
```bash
# 3) open/ 에서 하나 골라 claim — atomic
{{PY}} .project_manager/tools/board.py claim T-0003 --session session-A

# 4) 코드 작업

# 5) 다 끝나면 완료 처리 (회귀 통과 후 --tests-pass)
{{PY}} .project_manager/tools/board.py complete T-0003 --tests-pass

# 또는 막혔으면
{{PY}} .project_manager/tools/board.py block T-0003 --reason "외부 키 발급 대기"

# 막힘이 풀렸으면 다시 open 으로
{{PY}} .project_manager/tools/board.py unblock T-0003

# 잘못 잡았으면 원위치
{{PY}} .project_manager/tools/board.py unclaim T-0003
```

### 새 ticket 발행
```bash
{{PY}} .project_manager/tools/board.py new "모듈 X 구현 + 통합" \
    --touches src/x.py,tests/test_x.py \
    --depends T-0001 \
    --tag phase-1
```

### 조회
```bash
{{PY}} .project_manager/tools/board.py list                    # 전체 상태
{{PY}} .project_manager/tools/board.py list --status open      # open 만
{{PY}} .project_manager/tools/board.py list --tag phase-1      # phase-1 태그
{{PY}} .project_manager/tools/board.py show T-0003             # 한 ticket 상세
{{PY}} .project_manager/tools/board.py refresh                 # board.md 강제 재생성
```

## 디렉토리

```
tickets/
├── README.md               ← 이 파일
├── _template.md            ← 새 ticket 만들 때 board.py 가 복사
├── open/                   누구든 claim 가능 (depends_on 모두 done 일 때)
├── claimed/                작업 중 (frontmatter.claimed_by 가 세션 식별)
├── blocked/                의존성/외부 대기 — claim 불가
└── done/                   완료
```

## 충돌 회피

- **세션 시작 전 `board.py list` 확인** — 같은 파일을 건드리는 ticket 이 다른 세션에 claim 되어 있으면 다른 걸 골라라.
- **`touches:` 정확히 적기** — `git status` 가 회피의 마지막 보루.
- **claim 후 진행 없으면** — 다른 세션이 `unclaim` 후 새로 claim 가능 (운영 가이드, 자동화 X).

## ID 규칙

- 형식: `T-NNNN` — 4자리 zero-padded.
- 신규는 `board.py new` 가 자동 할당 (현재 최댓값 + 1).
- 파일명: `T-NNNN-<short-slug>.md`.

## 세션 식별

`claim` 시 `--session <name>` 으로 명시 (harness-무관·1순위 권장). 없으면 board.py 식별 우선순위:
1. `--session <name>` 인자
2. `$PM_SESSION_NAME` 환경변수 (있으면 · 구 `$CLAUDE_SESSION_NAME` = deprecated alias·여전히 인식)
3. `local.conf` 의 `session=`
4. 자동 생성 `<hostname>-<pid>`

## 보드 새로고침

`board.py` 의 모든 변경 명령 (`claim`, `complete`, `block`, `unclaim`,
`unblock`, `new`) 끝에 자동 refresh. 수동 강제는 `board.py refresh`.

## 한계 (의도된 단순성)

- **분산 락 아님.** 같은 파일시스템 한 클론에서만 atomic. 두 머신이 동시에 쓰면 깨질 수 있음 — 운영상 1 머신 기준.
- **자동 unclaim 없음.** TTL 기반 회수는 일부러 빼놨다. 사람이 결정.
- **의존성 그래프 검증.** `depends_on` 은 claim 시점에 한 번 검사. 순환 의존은 `board.py lint` 가 검출.
- **complete 동기화 게이트.** `board.py complete` 는 `done/` 으로 옮기기 전에 `log/current.md` entry · 회귀 통과 (`--tests-pass`) 를 확인하고 `status.md` 갱신 여부를 경고한다. 정당한 예외는 `--allow-missing-log` / `--allow-untested` 로 우회.
- **thin ticket 차단.** `board.py lint` 는 open/claimed 본문에 `_template.md` placeholder 가 남았거나 표준 섹션 (목표/완료 조건/참고) 이 없으면 실패한다.
