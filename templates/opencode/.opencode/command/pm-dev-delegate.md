---
description: "orchestrator(PM) dev/code-reviewer 위임 표준 절차 — 사전조건 + touches disjoint cross-check + domain 소환(domain affected). 위임 프롬프트 본문은 AGENTS.md §3.4(dev)/§3.5(reviewer) 단일 진실 참조. claim 은 별도(pm-wave-claim). Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
argument-hint: "T-NNNN [developer|code-reviewer]"
---

<command-instruction>

# /pm-dev-delegate T-NNNN [developer|code-reviewer] — orchestrator 위임

> {{PROJECT_NAME}} PM 의 ticket 구현/검토 위임 표준 절차. 위임 자체는 opencode
> 네이티브 `task` tool 로 한다 (AGENTS.md §3.1). 본 command 는 위임 *전* 사전조건
> 검증 + touches disjoint cross-check + domain 소환을 trigger 단위로 강제한다.
> 비즈니스 로직 0 — 절차 안내 + 엔진 CLI 호출.

ticket 번호는 `$ARGUMENTS` 에서 받는다 (예: `T-0007`). 역할(developer | code-reviewer)이
함께 오면 그 위임을, 없으면 dev → reviewer cycle 순으로 안내한다.

## 사전 조건 (위임 전 확인 · AGENTS.md §3.3)

- ticket 이미 claim (`pm-wave-claim` 통과 · `pm` 세션).
- depends_on 모두 done.
- touches 명시.
- DoD verify-able.
- **컨텍스트 예산** — touches 가 대형 파일·광범위 읽기를 요구하면 dev(cold subagent) truncation
  위험. 본문이 정확한 함수/라인·패턴 reference 로 읽기를 좁히는지 확인 (안 되면 위임 전 본문
  보강·분할).

## domain 소환 (recall — dev 위임 *전*)

위임 *전* ticket 의 covers 매칭 domain 페이지를 띄워 dev 에게 함께 넘긴다 (읽기 맥락 — dev 가
도메인 지식 없이 구현하는 걸 막음 · ADR-0018):

```bash
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN
```

- 출력 = ticket touches ∩ 페이지 `covers` 매칭 페이지. 줄 앞 `⚠ ` = **stale**(담당 코드가 페이지
  갱신 후 커밋됨).
- `(영향 domain 페이지 없음)` → 소환할 것 없음 · 생략.
- 매칭된 페이지 경로를 developer 위임 프롬프트에 인용/전달한다. **⚠ stale 페이지는 "맹신 말 것"**
  경고를 동반 — 담당 코드 변경 후 미갱신이라 정보가 상했을 수 있다.

## touches disjoint 안전성 cross-check (병렬 wave)

병렬 위임 (dev 여럿 동시) 시 위임 전 검증:

- 모든 claimed ticket 의 touches 가 *완전 disjoint* (file 겹침 0)? — *공통 통합 파일에 함수 단위
  추가* 는 완화 조건으로 OK.
- 같은 함수·같은 줄 동시 수정은 차단.
- baseline 회귀 측정은 *dev cycle 끝난 후 한 번에* (race 회피).

## 위임 실행 (task tool — AGENTS.md §3.1)

PM 이 내장 `task` tool 을 호출한다 (`subagent_type` · `description` · `prompt`). subagent 의
`tools:`/`permission:`/`model:` (`.opencode/agents/*.md`) 가 권한·모델을 정한다.

- **developer 위임 프롬프트** — AGENTS.md §3.4 가 단일 진실. (소환된 domain 페이지가 있으면 그
  경로를 프롬프트에 동반하고 ⚠ 표시분은 "맹신 말 것" 경고를 붙인다.)
- **code-reviewer 위임 프롬프트** — AGENTS.md §3.5 가 단일 진실. (status.md / log 갱신은 PM 담당 —
  그 누락은 dev must-fix 아님. 소환된 domain 페이지가 있으면 그 wiki DoD 반영 여부도 점검.)

> 표준 프롬프트 전문은 여기에 복제하지 않는다 — AGENTS.md §3.4/§3.5 를 참조한다 (single-source ·
> 복제는 stale 원천 · lean).

**task 병렬** — 병렬 위임이 필요하면 task tool 을 여러 번 호출한다. 병렬 실행은 opencode 가 자식
세션을 관리한다 (AGENTS.md §3.3). 별도 background 표현은 필요 없다.

> reviewer 위임과 **병행해 codex 외부 교차검증**을 돌릴 수 있다 (표준 리뷰 게이트):
> `{{PY}} .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
> (전제 `external_review_enabled=true`). 상세는 `pm_playbook.md` §"검토 루프".

## reviewer 후 PM 처리 (AGENTS.md §3.6)

- **PM 직접 fix** — 1줄·1패턴 · dev 안 도는 영역. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 dev 가 같은 file 작업 중.
- **별도 ticket 후보** — 본 ticket 범위 외 / 후속 caller 추가 시.
- **reviewer cross-check** — reviewer 도 틀릴 수 있다. should-fix 처리 전 코드 흐름 독립 점검 ·
  부정확이면 변경 불필요 + log 영구 기록. 특히 *reviewer 가 다른 ticket 영역의 결함을 현재 ticket
  영역으로 잘못 attribute* 가능 — PM 이 진짜 영역 확인 후 fix 분기 결정.

## 폴백 (task tool 미노출)

`task` tool 을 못 쓰는 환경(headless·CI)에선 외부 프로세스 진입으로 동일 위임을 한다 —
AGENTS.md §3.7 (`opencode run --agent build/plan`) 참조.

## 결정

- **board.py 조작은 orchestrator(PM)** — 위임 프롬프트에 명시. 서브에이전트는 구현/검토만.
- **위임 프롬프트는 한 줄** — ticket 본문이 self-contained 의무 → 추가 컨텍스트 불필요. 길어지면
  ticket 본문 보강.
- **single-source** — dev/reviewer 표준 프롬프트는 복제하지 않고 AGENTS.md §3.4/§3.5 참조 (ADR-0008
  lean).

## 참고

- `AGENTS.md` §3 — 위임 규약·task tool·dev/reviewer 표준 프롬프트 단일 진실
- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기 단일 진실
- `.opencode/agents/developer.md` · `.opencode/agents/code-reviewer.md` — subagent 정의

</command-instruction>

<user-request>
$ARGUMENTS
</user-request>
