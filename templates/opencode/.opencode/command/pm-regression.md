---
name: pm-regression
description: "비차단 백그라운드 회귀 — full 테스트를 run_in_background 로 pre-warm + 완료 알림, red 면 ticket 플래그. push 게이트(pre-push 훅)가 green 검증(`tests/` 가 있는 코드 repo 만 · PM 홈 push 는 lint 게이트만). dev 빠른 루프는 --ticket touches 스코프. Triggers: '회귀 돌려', '백그라운드 테스트', 'regression', 'pm-regression'."
audience: pm-internal
---

# /pm-regression — 비차단 백그라운드 회귀

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

- 회귀는 전체 suite 이며 코드 repo(`tests/` 가 있는 트리)는 green 만 push한다(pre-push 훅: `board.py regression check`). 분리 형상 PM 홈(board·wiki·엔진 사본만)의 push 는 회귀를 요구하지 않고 lint 게이트만 돈다.
- full 회귀는 이 skill로 백그라운드 pre-warm하고, dev 루프는 `--ticket` 스코프(advisory)를 쓴다.

상황별 운영 상세는 [references/operational-details.md](../../.claude/skills/pm-regression/references/operational-details.md)를 해당 상황에서 읽는다.

## 백그라운드 full 회귀

작업 한 단락 종료 또는 push 전에 기존 하네스의 백그라운드 기능으로 core를 실행한다. 검증 트리의 코드와 HEAD를 고정하고 다른 작업 트리의 독립 개발은 계속한다. 완료된 같은 HEAD·명령·작업공간의 전체 결과가 있으면 재사용한다. 해당 후보의 완료·통합 시 결과를 확인하며 pending이면 결과만 기다린다.

```bash
# Bash run_in_background: true 로 호출. 완료되면 하니스가 세션을 재호출(알림).
python3 .project_manager/tools/board.py regression run
```

- green → 해당 후보의 완료·통합과 일반 push의 core 증거로 사용한다. 선언 플랫폼 전체는 릴리즈 livegate record에서 같은 커밋으로 검증한다.
- red → 해당 후보의 완료·통합과 core 보호 push를 차단하고 원인을 수정한다. 다른 트리의 독립 개발은 계속한다(코드 repo 한정, PM 홈 push는 회귀 비대상).
- 개발 run/check는 현재 플랫폼의 test_cmd만 실행·판정하고 qa.platforms의 wrapper는 호출하지 않는다. Windows 전체 검증은 모든 구현·통합 후 릴리즈 직전에 실행한다. 새 scheduler나 작업 큐를 추가하지 않는다.
- 결과는 per-clone `.project_manager/.local/regression.json`에 HEAD 키로 기록된다.

## 이번 호출 인자

`$ARGUMENTS` — 사용자가 슬래시 명령 뒤에 준 값이다(하네스가 치환한다). 비어 있으면 인자 없이 부른 것이다. 이 카드가 인자를 받는 진입이면 그 값을 그대로 backbone 에 넘기고, 인자를 받지 않는 진입이면 값이 붙어 온 것을 사용자에게 알리고 인자 없이 다시 부르게 안내한다.
