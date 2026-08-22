---
name: pm-qa
description: "통합 검증 게이트 — 회귀(pytest) + board.py lint + git status/최근 commit 을 한 trigger 로 묶어 단일 PM report. wave 종료 직전 / wave 시작 baseline / ticket 완료 / 사용자 'qa·회귀 확인·통합 검증' 시. red 면 후속 단계 중단(fail-soft 아님). Triggers: '통합 검증', 'qa', '회귀 확인', 'wave 종료 검증', 'baseline 측정', 'pm-qa'."
audience: pm-internal
---

# $pm-qa — 통합 검증 게이트

wave 종료 직전·시작 baseline 또는 사용자 `"qa·회귀 확인·통합 검증"` 요청 시 회귀+lint+git을 foreground로 합쳐 즉시 진행/중단을 판단한다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 인접 스킬

- **pm-qa**: foreground 합성 게이트. wave 경계에서 회귀+lint+git report 후 즉시 판단.
- [[pm-regression]]: background 회귀 pre-warm(비동기·알림), 기다리지 않는 dev loop.
- [[pm-wave-finish]]: `ticket_finish`·board complete로 단일 ticket 종료.
- [[pm-bootstrap]]: 세션 시작의 board/git/회귀 측정.

wave 시작은 **baseline fix → wave 시작** 순서이며 red baseline 위에 wave를 쌓지 않는다. wave 종료 직전에는 `$pm-wave-finish` 전에 실행한다.

## 실행 순서

### 1. 회귀 측정 (foreground)

```bash
# 프로젝트 test 명령은 board regression 이 해소·기록한다 (local.conf test.cmd= · rc0 만 pass)
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
