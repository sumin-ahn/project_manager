---
name: pm-dev-delegate
description: "orchestrator dev/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check + background 옵션. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
---

# /pm-dev-delegate T-NNNN [--role developer|code-reviewer] [--background] — orchestrator 위임

> {{PROJECT_NAME}} PM 의 orchestrator 위임 표준 프롬프트. Agent 툴 +
> `subagent_type: developer|code-reviewer` + `run_in_background` 옵션. ticket
> 본문이 self-contained 의무 충족 시 위임 프롬프트는 한 줄.

## 사전 조건

- ticket 이미 claim (`pm-wave-claim` 통과·`pm` 세션명).
- depends_on 모두 done.
- touches 명시.
- DoD verify-able.
- **컨텍스트 예산 확인** — touches 대형 파일·광범위 읽기 필요 시 dev truncation 위험. 미리 분할했거나 본문이 정확한 함수/라인·패턴 reference 로 dev 읽기를 좁히는지 확인 (안 되면 위임 전 본문 보강·분할).

## domain 소환 (recall — dev 위임 *전*)

위임 *전* ticket 의 covers 매칭 domain 페이지를 띄워 dev 에게 함께 넘긴다 (읽기 맥락 — dev 가 도메인 지식 없이 구현하는 걸 막음·ADR-0018 §7b):

```bash
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN
```

- 출력 = ticket touches ∩ 페이지 `covers` 매칭 페이지. 줄 앞 `⚠ ` = **stale**(담당 코드가 페이지 갱신 후 커밋됨).
- `(영향 domain 페이지 없음)` → 소환할 것 없음·생략.
- 매칭된 페이지 경로를 아래 developer 위임 프롬프트에 인용/전달한다. **⚠ stale 페이지는 "맹신 말 것"** 경고를 동반 — 담당 코드 변경 후 미갱신이라 정보가 상했을 수 있다(enforcement 아닌 visibility·Q3).

## 실행 패턴

### developer 위임

```
Agent 툴 호출:
  description: "T-NNNN implement"
  subagent_type: developer
  run_in_background: true (병렬 wave 시) | false (직렬·이 결과에 의존 시)
  prompt:
    "T-NNNN 을 구현하라.

     세션명: orch-dev-TNNNN (board.py 조작은 orchestrator(PM) 담당·dev 는 코드+테스트만).

     ticket 본문은 {{PY}} .project_manager/tools/board.py show T-NNNN 로 확인.
     본문이 self-contained — 목표/인터페이스/결정/DoD/참고 절 대로 구현.
     (PM 첨부 — 소환된 domain 페이지: <domain affected 출력 경로·있으면>. ⚠ 표시분은 stale 이니 맹신 말 것.)

     완료 시 보고:
     - 변경 파일 목록
     - 신규 테스트 수
     - 전체 회귀 결과 (A / B passed)
     - DoD 각 항목별 충족 evidence 명시"
```

### code-reviewer 위임

```
Agent 툴 호출:
  description: "T-NNNN review"
  subagent_type: code-reviewer
  run_in_background: true (병렬 reviewer 시) | false (단일 reviewer 시)
  prompt:
    "T-NNNN 의 변경을 검토하라.

     변경 파일: <touches 인자 그대로 인용>.

     ⚠️ status.md / log/current.md 갱신은 orchestrator(PM) 담당 — 그 누락은 developer
     must-fix 아님.
     소환된 domain 페이지가 있으면 그 wiki DoD(touch∩covers 갱신·T-0081 soft step) 반영 여부도 점검.

     완료 시 보고:
     - must-fix (수정 필수·{{PROJECT_CONSTRAINTS}} 위반·결함)
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시"
```

> ⚙️ reviewer 위임과 **병행해 codex 외부 교차검증**을 돌린다 (표준 리뷰 게이트):
> `{{PY}} .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
> (ADR 본문 정합 필요 시 `--paths` 에 **코드 경로+ADR 함께 나열** — `--paths` 는
> `--ticket` touches 를 *대체*함). 전제
> `external_review_enabled=true`. 상세는 `pm_playbook.md` §"검토 루프".

## touches disjoint 안전성 cross-check (병렬 wave)

병렬 wave (dev N 동시 spawn) 시 PM 이 위임 전 검증:

- 모든 claimed ticket 의 touches 가 *완전 disjoint* (file 겹침 0)? — *공통 통합 파일 함수 단위 추가* 는 완화 조건으로 OK.
- 같은 함수·같은 줄 동시 수정은 차단.
- baseline 회귀 측정은 *dev cycle 끝난 후 한 번에* (race 회피).

## must-fix 분기 (reviewer 후)

reviewer 보고 후 PM 처리:

- **PM 직접 fix** — 1줄·1패턴·dev 안 도는 영역. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 dev 가 같은 file 작업 중.
- **별도 ticket 후보 메모** — 본 ticket 범위 외 / 후속 caller 추가 시.
- **suggestion 보류** — 운영 영향 0·기능 충분.

## reviewer 분석 cross-check

reviewer 도 항상 옳지 않다. PM 가 should-fix 처리 전 *코드 흐름 자체* 독립
점검·부정확이면 변경 불필요 + log/current.md 영구 기록. 특히 *reviewer 영역 attribute
부정확* — reviewer 가 *다른 ticket 영역의 결함을 현재 ticket 영역으로 잘못
attribute* 가능. PM 이 진짜 영역 확인 후 fix 분기 결정.

## 결정

- **board.py 조작은 orchestrator(PM)** — 위임 프롬프트에 명시. 서브에이전트는 구현/검토만.
- **dev 자기 보고 표준 형식 강제** — 위임 프롬프트에 *DoD 각 항목별 충족 evidence* 명시 요구.
- **background 우선** — 병렬 wave 효율 ↑. 단 검토 결과에 다음 ticket 의존 시 foreground.
- **위임 프롬프트는 한 줄** — ticket 본문이 self-contained 의무 → 추가 컨텍스트 불필요. 길어지면 ticket 본문 보강.

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기 단일 진실
- `.claude/agents/developer.md` — developer 서브에이전트 정의
- `.claude/agents/code-reviewer.md` — code-reviewer 서브에이전트 정의
