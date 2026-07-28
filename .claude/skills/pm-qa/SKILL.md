---
name: pm-qa
description: "통합 검증 게이트 — 회귀(pytest) + board.py lint + git status/최근 commit 을 한 trigger 로 묶어 단일 PM report. wave 종료 직전 / wave 시작 baseline / ticket 완료 / 사용자 'qa·회귀 확인·통합 검증' 시. red 면 후속 단계 중단(fail-soft 아님). Triggers: '통합 검증', 'qa', '회귀 확인', 'wave 종료 검증', 'baseline 측정', 'pm-qa'."
audience: pm-internal
---

# /pm-qa — 통합 검증 게이트

wave 종료 직전·시작 baseline 또는 사용자 `"qa·회귀 확인·통합 검증"` 요청 시 회귀+lint+git을 foreground로 합쳐 즉시 진행/중단을 판단한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 인접 스킬

- **pm-qa**: foreground 합성 게이트. wave 경계에서 회귀+lint+git report 후 즉시 판단.
- [[pm-regression]]: background 회귀 pre-warm(비동기·알림), 기다리지 않는 dev loop.
- [[pm-wave-finish]]: `ticket_finish`·board complete로 단일 ticket 종료.
- [[pm-bootstrap]]: 세션 시작의 board/git/회귀 측정.

wave 시작은 **baseline fix → wave 시작** 순서이며 red baseline 위에 wave를 쌓지 않는다. wave 종료 직전에는 `pm-wave-finish` 전에 실행한다.

## 실행 순서

### 1. 회귀 측정 (foreground)

```bash
# 프로젝트 test 명령은 board regression 이 해소·기록한다 (local.conf test_cmd= · rc0 만 pass)
python3 .project_manager/tools/board.py regression run
```

성공은 `N passed in T.Ts`. red면 즉시 PM에게 보고하고 후속 wave 종료/시작을 중단한다.

### 2. board.py lint (foreground)

```bash
python3 .project_manager/tools/board.py lint
```

성공은 clean(또는 advisory만). 차단성 warning(의존성 모순·placeholder 잔존·dangling wikilink)은 PM에게 보고한다.

### 3. git status / 최근 commit (foreground · 1번과 병렬 가능)

```bash
git status -s
git log --oneline -5
```

working tree clean 여부·변경 파일 수·최근 commit 정합(핸드오프 commit 누락)을 확인한다.

### 4. (선택) 프로젝트 evidence summary

운영 데이터(cron 로그·paper-run audit 등)가 있으면 인스턴스 overlay로 최근 cycle 요약을 덧붙인다. 없으면 noise를 피하려 skip하며 구체 경로는 인스턴스 소유다.

### 5. PM report

호출자가 다음 markdown을 합산 출력한다.

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

## 불변

- fail-soft가 아니다. red는 즉시 보고하고 후속 단계를 중단한다.
- 1번 회귀와 3번 git은 독립이므로 multiple Bash 병렬 호출할 수 있다.
- 비즈니스 로직 없는 thin 합성이며 실제 차단 검증은 push gate(pre-push hook)가 보증한다.
- evidence는 선택·인스턴스 소유다.
- 참고: [[pm-regression]] · [[pm-wave-finish]] · [[pm-bootstrap]]; backbone CLI `python3 .project_manager/tools/board.py {lint,regression}`.
