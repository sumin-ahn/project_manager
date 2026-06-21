---
description: "PM 세션 종료 핸드오프 7단계 자동화 — log entry skeleton append + pm_state.md sliding window 정리 + 인계 프롬프트 stdout + 회귀 측정 + git status. backbone CLI .project_manager/tools/pm_handoff.py thin wrapper. Triggers: '핸드오프', '인계', 'PM 세션 종료', 'pm-handoff'."
argument-hint: "--session-num <N> --wave-summary \"<요약>\""
---

<command-instruction>

# /pm-handoff — PM 세션 종료 핸드오프 자동화

> {{PROJECT_NAME}} PM 세션의 핸드오프 7단계 (pm_role.md §"핸드오프 절차") 를
> 한 trigger 로 처리한다. PM 손은 *log/current.md 본문 서술 + 인계 프롬프트 §핵심
> 인계 사항 채움 + git commit* 만 남는다. backbone =
> `.project_manager/tools/pm_handoff.py`. 비즈니스 로직 0 — 엔진 CLI 호출 thin wrapper.

## 사용 시점

다음 중 하나면 호출:
- 사용자 명시 종료 신호 (*"세션 종료"·"인계해"*)
- PM 컨텍스트 부족 신호 (자기 보고) — opencode 200K 압박 시
- wave 마지막 commit 후 자연 종료 시점

## 실행

opencode bash tool 로 실행. 사용자가 준 인수(`$ARGUMENTS`)에서 세션 번호·wave 요약을 추출해
아래 형태로 호출한다. 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일·C2 콘솔 reconfigure)하므로
env prefix 불필요 — Windows/CP949·PowerShell 서도 env 없이 동작. 드물게 필요하면 셸별 문법
(PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`).

```bash
{{PY}} .project_manager/tools/pm_handoff.py \
  --session-num <N> \
  --wave-summary "<wave 1~3 한 줄 요약>"
```

> `--session-num` 은 **숫자만**(`19`) — CLI 가 "차" 를 붙여 `PM 19차` 로 포맷한다(`19차` 줘도
> 후행 "차" 정규화로 이중부착 방지·T-0100).

옵션:
- `--dry-run` — log/current.md / pm_state.md 변경 미적용·stdout 미리보기만.
- `--no-pytest` — 회귀 측정 skip (직전 wave 종결 commit 의 숫자 신뢰 시·**비권장**).

## CLI 자동 처리 단계

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단·핸드오프 불가 (baseline fix 후 재시도).
2. **log/current.md handoff entry skeleton append** — `## [YYYY-MM-DD] handoff | PM N차 → 다음 PM 세션` 형식. 본문 = `<PM 손 채움>`.
3. **pm_state.md sliding window 정리** — §세션 식별 표에 N차 entry 추가 + 가장 오래된 entry 제거.
4. **pm_state.md 길이 검증** — `wc -l` 700 라인 초과 시 warning. + log/current.md entry 가 임계(40) 초과면 `pm_log.py archive` 권장 warning.
5. **인계 프롬프트 stdout 출력** — 고정부 채움. `<핵심 인계 사항>` 절은 PM 손.
6. **git status dump** — `git status -s` 출력 + 변경 파일 카운트.
7. **잔여 PM 수동 작업 checklist 출력**.

## 잔여 PM 손작업 (CLI 후)

1. **log/current.md handoff entry 본문 서술 (lean 스키마 — ADR-0008)** — skeleton 의 `<...>` placeholder 를 채움. 파생 가능 상태는 source 에 미룬다(point, don't copy). 비파생 salient 레이어만: **읽기 범위**(이 entry + 인용할 과거 entry/ADR 포인터) · **메타 학습**(ticket 상태에서 도출 불가한 교훈만·없으면 "없음") · **다음 intent**(두 줄 세분 — ADR-0008·T-0047: *대화 thread-tail* = 정지 직전 사용자 발화 + *pending user intent* = 다음 우선순위·사용자 결정 대기. 단 opencode 어댑터는 thread-tail 자동 추출 미구현=2차 — 현재 PM 손) · **회귀/incident**(회귀 "N passed / 상태" 1줄 baseline·green 도 + 비-자명 incident). **FORBIDDEN (대량 재열거 금지):** board 카운트·open ticket 목록·commit 해시·push 상태·직전 complete 산출물 재요약 (→ board·git·/pm-bootstrap·인접 entry 가 답함).
2. **인계 프롬프트 §핵심 인계 사항 절 채움** — 위 lean 스키마와 동일 (읽기범위·메타학습·다음intent + 회귀/incident 1줄). board 상태·open 목록·commit 해시 대량 재열거 금지.
3. **git commit** — 핸드오프 commit message 형식: `PM 세션(N차) 핸드오프 — pm_state.md sliding window + log/current.md handoff entry + PM (N+1)차 인계`.
4. **마지막 응답에 인계 프롬프트 코드블록 출력** — 사용자가 복사해 새 PM 세션에 붙여넣음.

## 결정

- **thin wrapper** — command 자체 비즈니스 로직 0·CLI 호출만.
- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. 핸드오프 후 신규 PM 이 broken state 로 시작 회피.
- **sliding window 정리는 자동** — 표 편집 race 회피 위해 CLI 가 직렬 처리.

## 참고

- `.project_manager/tools/pm_handoff.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 핸드오프 절차 7단계 단일 진실
- `AGENTS.md` — opencode PM 핸드오프·엔진 호출 규약

</command-instruction>

<user-request>
$ARGUMENTS
</user-request>
