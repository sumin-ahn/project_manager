---
name: pm-regression
description: "비차단 백그라운드 회귀 — full 테스트를 run_in_background 로 pre-warm + 완료 알림, red 면 ticket 플래그. push 게이트(pre-push 훅)가 green 검증. dev 빠른 루프는 --ticket touches 스코프. Triggers: '회귀 돌려', '백그라운드 테스트', 'regression', 'pm-regression'."
audience: pm-internal
---

# $pm-regression — 비차단 백그라운드 회귀

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

- 회귀는 전체 suite 이며 green 만 push한다(pre-push 훅: `board.py regression check`).
- full 회귀는 이 skill로 백그라운드 pre-warm하고, dev 루프는 `--ticket` 스코프(advisory)를 쓴다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 백그라운드 full 회귀

작업 한 단락 종료 또는 push 전에 하니스 background 로 실행하고 PM 은 다른 일을 계속한다.

```bash
# Bash run_in_background: true 로 호출. 완료되면 하니스가 세션을 재호출(알림).
python3 .project_manager/tools/board.py regression run
```

- green → 다음 push 즉시 통과.
- red → `regression check` 가 push 차단 → 원인을 고친다.
- 결과는 per-clone `.project_manager/.local/regression.json`에 HEAD 키로 기록된다.
