---
description: "통합 검증 게이트 — 회귀(pytest) + board.py lint + git status/최근 commit 을 한 trigger 로 묶어 단일 PM report. wave 종료 직전 / wave 시작 baseline / ticket 완료 / 사용자 'qa·회귀 확인·통합 검증' 시. red 면 후속 단계 중단(fail-soft 아님). Triggers: '통합 검증', 'qa', '회귀 확인', 'wave 종료 검증', 'baseline 측정', 'pm-qa'."
---

<command-instruction>

# /pm-qa — 통합 검증 게이트

> {{PROJECT_NAME}} PM 이 wave 경계(종료 직전·시작 baseline)에서 손작업 검증 명령 4~5개를 **한 trigger** 로
> 묶어 단일 PM report 를 낸다. **foreground 합성 게이트** — 결과를 보고 wave 진행/중단을 즉시 판단. 비즈니스
> 로직 0 — 엔진 CLI/회귀/git 호출 thin wrapper.

## 인접 command 와 구분
- **pm-qa (이 command)** = *foreground* 합성 게이트. wave 경계에서 회귀+lint+git 을 한 번에 묶어 report·즉시 판단.
- `/pm-wave-finish` = ticket 완료 부기(ticket_finish·board complete). 단일 ticket 종료.
- `/pm-bootstrap` = 세션 *시작* 시 board/git/회귀 측정.
- 백그라운드 회귀 pre-warm 은 harness-별(claude `/pm-regression`) — opencode 는 push 게이트(pre-push 훅)가 green 보증.

## 사용 시점
- **wave 종료 직전** — `/pm-wave-finish` 호출 전 baseline 확인.
- **wave 시작 baseline** — *baseline fix → wave 시작* 패턴(red baseline 위에 wave 쌓지 않기).
- 사용자 명시 *"qa·회귀 확인·통합 검증"* 시.

## 실행 순서

### 1. 회귀 측정 (foreground)
```bash
{{TEST_CMD}} 2>&1 | tail -5
```
성공 = `N passed in T.Ts`. red → 즉시 PM 에게 보고 + **후속 단계 중단 검토**.

### 2. board.py lint (foreground)
```bash
{{PY}} .project_manager/tools/board.py lint
```
성공 = clean(또는 advisory만). 차단성 warning(의존성 모순·placeholder 잔존·dangling wikilink) 있으면 PM 보고.

### 3. git status / 최근 commit (foreground · 1번과 병렬 가능)
```bash
git status -s
git log --oneline -5
```

### 4. (선택) 프로젝트 evidence summary
운영 데이터(cron 로그·audit 등)가 있는 프로젝트는 인스턴스 overlay 로 최근 cycle 요약을 덧붙인다(없으면 skip).

### 5. PM report (호출자가 markdown 합산 출력)
```
## PM 통합 검증 report (YYYY-MM-DD HH:MM)
- 회귀: N / N 통과 (또는 K failed — <첫 fail 1줄>)
- lint: clean (또는 N advisory / 차단 M)
- git: <clean | N files modified> · branch <name> · HEAD <SHA short>
- 최근 commit: <SHA> <subject>
- (선택) evidence: <last cycle summary>

## 결정 (PM 손)
- 회귀 통과 + lint 차단 0 + working tree clean → wave 종료/시작 OK.
- 회귀 red → baseline fix 또는 dev 재작업.
- working tree dirty → wave 종결 commit 누락·재확인.
```

## 결정
- **fail-soft 가 아니다** — red 시 즉시 보고 + 후속 단계 중단.
- **병렬 가능 단계 명시** — 1번(회귀)과 3번(git)은 독립.
- **thin** — 비즈니스 로직 0. 진짜 차단 검증은 push 게이트(pre-push 훅)가 보증.
- **evidence 단계는 선택·인스턴스 소유** — 운영 데이터 유무는 프로젝트마다 다름.

</command-instruction>
