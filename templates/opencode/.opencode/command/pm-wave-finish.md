---
description: "wave 안 ticket 완료 부기 — ticket_finish.py wrapper + 회귀 측정 + log/current.md skeleton + board complete + git stage. 모듈 판정·비고·log/current.md 서술·git commit 은 PM 손. Triggers: 'T-NNNN 완료', 'ticket 정리', 'finish', 'pm-wave-finish'."
argument-hint: "T-NNNN"
---

<command-instruction>

# /pm-wave-finish T-NNNN — wave ticket 완료 부기

> {{PROJECT_NAME}} PM wave 안 ticket 완료 시 부기 자동화. backbone =
> `.project_manager/tools/ticket_finish.py`. 본 command 는 호출 chain
> + PM 손 잔여 작업 안내. 비즈니스 로직 0 — 엔진 CLI 호출 thin wrapper.

ticket 번호는 `$ARGUMENTS` 에서 받는다 (예: `T-0007`).

## 사용 시점

dev/reviewer cycle 통과 (must-fix 0) 또는 PM 직접 구현 ticket 완료 시.

## 실행

opencode bash tool 로 실행. `--section` 인자는 **deprecated no-op**(ADR-0023 — status.md 합계표
제거로 더 이상 쓰지 않음·후방호환 수용만). 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일·C2 콘솔
reconfigure)하므로 env prefix 불필요 — Windows/CP949·PowerShell 서도 env 없이 동작. 드물게 필요하면
셸별 문법(PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`).

```bash
{{PY}} .project_manager/tools/ticket_finish.py T-NNNN
```

## CLI 자동 처리

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단 (반려 → dev 재작업 필요).
2. **log/current.md complete entry skeleton append** — `## [YYYY-MM-DD] complete | T-NNNN — <title>` 형식. 본문 = `<PM: 무엇을·왜>` placeholder.
3. **board.py complete T-NNNN** — `--tests-pass` 가드 통과 후 status open→done.
4. **git stage** — 변경 파일 자동 `git add`. commit 은 별도 (PM 손).

> status.md 는 건드리지 않는다 — judgment-only(ADR-0023)·테스트 수는 박제 안 함(pytest 실측·history 는 log).

## 잔여 PM 손작업 (CLI 후)

1. **status.md 모듈 *판정/비고*** — architect content-truth·PM 점검(ADR-0022/0023). 모듈 상태가 바뀌었으면 architect 가 *코드 대조*로 갱신·PM 점검. **테스트 수는 박제하지 않는다**(pytest 실측). CLI 자동화 안 함.
2. **log/current.md complete entry 본문 서술** — skeleton 의 `<PM: 무엇을·왜>` 를 실제 내용으로:
   - 변경 파일 목록
   - 단위 테스트 수·증가량
   - dev/reviewer cycle 요약 (must-fix·should-fix 처리 분기)
   - PM 직접 처리 should-fix (1줄·dev 안 도는 영역)
   - 메타 학습 (wave 다음 단계·후속 ticket 후보)
   - spec/ADR 정합 갱신 (있으면)
3. **git commit** — 메시지: `T-NNNN — <title 요약>` (또는 wave 단위 단일 commit·복수 ticket 일 때).
4. **wave 진행 중이면 다음 ticket** — `/pm-wave-claim` 으로 다음.
5. **wave 종결이면 wave 메타 entry append** — pm_playbook.md §"Wave 메타 학습 누적" 표준.

## 결정

- **thin wrapper** — 모듈 판정·commit 자동화 안 함 (의도적). 현재-진실 doc(status 판정) 직접 편집·자동 commit 의 부수 영향 회피. *자동화는 잡일까지·판정/서술/commit 은 architect/PM 손* 패턴 정합.
- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. ticket complete 차단 (board.py complete 의 `--tests-pass` 가드).
- **wave 종결 commit message 형식** — `PM 세션(N차) wave M — <ticket 목록> + <핵심 메타 학습 요약>`. wave 단위 단일 commit.

## 참고

- `.project_manager/tools/ticket_finish.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — wave 패턴 단일 진실
- `AGENTS.md` — opencode PM wave·엔진 호출 규약

</command-instruction>

<user-request>
$ARGUMENTS
</user-request>
