---
name: pm-regression
description: "비차단 백그라운드 회귀 — full 테스트를 run_in_background 로 pre-warm + 완료 알림, red 면 ticket 플래그. push 게이트(pre-push 훅)가 green 검증. dev 빠른 루프는 --ticket touches 스코프. Triggers: '회귀 돌려', '백그라운드 테스트', 'regression', 'pm-regression'."
---

# /pm-regression — 비차단 백그라운드 회귀 (D5=B)

> 회귀를 PM 이 기다리지 않게 한다. **full 회귀는 백그라운드로 pre-warm** 하고, push 시점엔 보통
> 이미 green. **dev 작업 중엔 ticket 스코프**로 빠른 피드백. (모델: 동시 다중 PM ADR §회귀)

## 두 경로
- **회귀 = 전체 suite** · green 인 것만 push (pre-push 훅이 `board.py regression check`).
- **백그라운드 pre-warm** = 이 skill. **티켓별 dev 루프** = `--ticket` 스코프(advisory).

## 백그라운드 full 회귀 (pre-warm + 알림)

작업이 한 단락 끝났거나 push 전에 — 하니스 background 로 돌리고 PM 은 계속 다른 일:

```bash
# Bash run_in_background: true 로 호출. 완료되면 하니스가 세션을 재호출(알림).
{{PY}} .project_manager/tools/board.py regression run
```
- green → 다음 push 즉시 통과. red → `regression check` 가 push 차단 → 원인 fix.
- 결과는 per-clone `.project_manager/.local/regression.json` (HEAD 키)에 기록.

## 티켓별 빠른 루프 (dev · advisory)

구현 중 자기 ticket 관련 테스트만 — 빠른 피드백 (push 게이트 아님):

```bash
{{PY}} .project_manager/tools/board.py regression run --ticket T-PAY-001   # touches → pytest -k
```

## red 처리

- 백그라운드 red 알림 → 해당 ticket 인지 + 고치거나 `board.py reopen` / follow-up ticket.
- **flaky** 면 `.project_manager/quarantine.txt` 에 test node id 추가 (회귀가 `--deselect`).
  격리는 임시 — 근본 원인 ticket 을 같이 발행할 것.
