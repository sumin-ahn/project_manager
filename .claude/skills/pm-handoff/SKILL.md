---
name: pm-handoff
description: "PM 세션 종료 핸드오프 7단계 자동화 — log entry skeleton append + pm_state.md sliding window 정리 + 인계 프롬프트 stdout + 회귀 측정 + git status. backbone CLI .project_manager/tools/pm_handoff.py thin wrapper. Triggers: '핸드오프', '인계', 'PM 세션 종료', 'pm-handoff'."
---

# /pm-handoff — PM 세션 종료 핸드오프 자동화

> {{PROJECT_NAME}} PM 세션의 핸드오프 7단계 (pm_role.md §"핸드오프 절차") 를
> 한 trigger 로 처리한다. PM 손은 *log/current.md handoff entry 본문 서술 + git commit* 만 남는다
> (인계 프롬프트는 트리거 축소 — 인계 본문은 다음 세션 부트스트랩이 log entry 에서 dump·ADR-0035).
> backbone = `.project_manager/tools/pm_handoff.py`.

## 사용 시점

다음 중 하나면 호출:
- 사용자 명시 종료 신호 (*"세션 종료"·"인계해"*)
- PM 컨텍스트 < 10% 신호 (자기 보고)
- wave 마지막 commit 후 자연 종료 시점

## 실행

```bash
{{PY}} .project_manager/tools/pm_handoff.py \
  --session-num <N> \
  --wave-summary "<wave 1~3 한 줄 요약>"
```

> `--session-num` 은 **숫자만**(`19`) 준다 — CLI 가 "차" 를 붙여 `PM 19차` 로 포맷한다.
> `19차` 를 줘도 CLI 가 후행 "차" 를 정규화(idempotent·T-0100)해 이중부착(`19차차`)을 막는다.

옵션:
- `--dry-run` — log/current.md / pm_state.md 변경 미적용·stdout 미리보기만.
- `--no-pytest` — 회귀 측정 skip (직전 wave 종결 commit 의 숫자 신뢰 시·**비권장**).

## CLI 자동 처리 단계

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단·핸드오프 불가 (baseline fix 후 재시도).
2. **log/current.md handoff entry skeleton append** — `## [YYYY-MM-DD] handoff | PM N차 → 다음 PM 세션` 형식. 본문 = `<PM 손 채움>`.
3. **pm_state.md sliding window 정리** — §세션 식별 표에 N차 entry 추가 + 가장 오래된 entry 제거. 자세히 → pm_role.md §핸드오프 절차 #4.
4. **pm_state.md 길이 검증** — `wc -l` 700 라인 초과 시 warning (과거 누적 정리 누락 신호). + log/current.md entry 가 임계(40) 초과면 `pm_log.py archive` 권장 warning.
5. **인계 프롬프트(트리거) stdout 출력** — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)" 의 트리거(역할 framing + `/pm-bootstrap`). **인계 본문은 채우지 않는다** — log entry 가 carry·다음 세션 부트스트랩이 자동 dump(차수·인계 본문·남은작업·T-0179·ADR-0035).
6. **git status dump** — `git status -s` 출력 + 변경 파일 카운트.
7. **잔여 PM 수동 작업 checklist 출력**.

## 잔여 PM 손작업 (CLI 후)

1. **log/current.md handoff entry 본문 서술 (lean 스키마 — ADR-0008)** — skeleton 의 `<...>` placeholder 를 채움. handoff 는 *파생 가능한 상태를 source 에 미룬다(point, don't copy)* — 떠나는 세션만이 싸게 줄 수 있는 비파생 salient 레이어 3섹션만:
   - **읽기 범위** — 이 entry + 인용할 과거 entry/ADR 의 *포인터* (라인수·전체Read 아님).
   - **메타 학습** — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".
   - **다음 intent** — 두 줄로 세분(ADR-0008 재검토 트리거·T-0047):
     - **대화 thread-tail** — 정지 직전 사용자 발화. ctx-trigger 경로는 어댑터 훅이 transcript 에서 자동 추출(초안). 대화형은 PM 손.
     - **pending user intent** — 다음 우선순위 + 사용자 결정 대기. PM 손.
   - **회귀/incident** — 회귀 "N passed / 상태" **1줄(green 도 — baseline)** + 비-자명 incident. 회귀는 1줄 load-bearing 이라 항상 적는다(pm_bootstrap default skip 의 "handoff 참조" 안내와 정합).
   - **정지 후 thread-tail 검토** — ctx-trigger 자동 채움분은 *초안*이다. 새 PM 이 슬롯을 검토·편집한다(민감 발화 노출 최소화·과/부족 추출 보정).
   - **FORBIDDEN (대량 재열거 금지 — source 가 답한다):** board done/open/claimed/blocked 카운트 (→ `board.py list`) · open ticket ID 목록 (→ `pm_bootstrap`) · commit 해시·push 상태 (→ `git log`/`git status`) · 직전 complete entry 산출물 재요약 (→ 인접 entry. "읽기 범위" 로 가리켜라). 재열거는 중복이고 중복은 stale 화로 거짓말한다. (회귀 1줄 baseline 은 예외 — 위 회귀/incident 라인에 유지.)
2. **domain capture (채록) 검토** — `{{PY}} .project_manager/tools/domain.py capture --tickets <이 세션 done ticket 들>` 실행. 출력의 *영향 페이지*(`⚠ `=stale) 와 *coverage gap*(담당 페이지 없는 touched 경로)을 보고 관련 domain 페이지를 갱신하거나 신규 scaffold 한다. **surface-only** — 도구는 *무엇을 갱신/신설할지 띄울 뿐*, 본문 자동생성·`updated:` 자동스탬프는 안 한다(stale 탐지 거짓 방지·ADR-0018 §7b). 갱신할 것 없으면 생략.
3. **git commit** — 핸드오프 commit message 형식: `PM 세션(N차) 핸드오프 — pm_state.md sliding window + log/current.md handoff entry + PM (N+1)차 인계`. trailer `Co-Authored-By: Claude`.
4. **마지막 응답에 인계 프롬프트(트리거) 코드블록 출력** — 다음 세션은 `/pm-bootstrap` 실행(트리거 붙여넣기 or 직접). 인계 본문은 부트스트랩이 log entry 에서 자동 dump 하므로 손-채움 불요(ADR-0035).

## 결정

- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. 핸드오프 후 신규 PM 이 broken state 로 시작 회피.
- **sliding window 정리는 자동** — 표 편집 race 회피 위해 CLI 가 직렬 처리.

## 참고

- `.project_manager/tools/pm_handoff.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 핸드오프 절차 7단계 단일 진실
