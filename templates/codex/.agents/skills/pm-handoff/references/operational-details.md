# $pm-handoff 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## CLI 자동 처리 단계

0. **dirty-tree 게이트** — PM 홈 + 활성 worktree 전수에서 미커밋 잔여(tracked 수정 ∪ untracked-unignored·gitignored 제외)를 판정한다. 잔여가 있으면 어떤 파일도 건드리기 전에 rc 1 차단 + 목록 열거 — **세션 산출을 먼저 커밋**하고 재실행한다(불가피하면 `--ack-dirty "<사유>"`). 커밋 0 트리는 untracked 만으로 판정하고, 비-git 트리는 비차단 경고다.
1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단·핸드오프 불가 (baseline fix 후 재시도).
2. **log/current.md handoff entry skeleton append** — `## [YYYY-MM-DD] handoff | PM N차 → 다음 PM 세션` 형식. 본문 = `<PM 손 채움>`.
3. **pm_state.md sliding window 정리** — §세션 식별 표에 N차 entry 추가 + 가장 오래된 entry 제거. 자세히 → pm_role.md §핸드오프 절차 #4.
4. **pm_state.md 길이 검증** — `wc -l` 700 라인 초과 시 warning (과거 누적 정리 누락 신호). + log/current.md entry 가 임계(40) 초과면 `pm_log.py archive` 권장 warning.
5. **인계 프롬프트(트리거) stdout 출력** — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)" 의 트리거(역할 framing + `$pm-bootstrap`). **인계 본문은 채우지 않는다** — log entry 가 carry·다음 세션 부트스트랩이 자동 dump(차수·인계 본문·남은작업).
6. **git status dump** — `git status -s` 출력 + 변경 파일 카운트.
7. **잔여 PM 수동 작업 checklist 출력**.

## 잔여 PM 손작업 (CLI 후)

1. **log/current.md handoff entry 본문 서술 (lean 스키마)** — skeleton 의 `<...>` placeholder 를 채운다. 파생 가능한 상태는 source 에 미루고 다음 비파생 salient 레이어만 쓴다.
   - **이 세션 박제 entries** — 기계 자동(직전 자기 handoff 이후의 complete/checkpoint 헤더
     목록·경계 미해소 시 최근 N건 표기). 본문 재요약 금지 — log 원문이 서사의 단일 진실.
   - **메타 학습** — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".
   - **pending user intent** — 다음 우선순위 + 사용자 결정 대기. PM 손.
   - **회귀/incident** — 회귀 "N passed / 상태" **1줄(green 도 — baseline)** + 비-자명 incident. 회귀는 항상 적는다.
   - **FORBIDDEN (대량 재열거 금지 — source 가 답한다):** board done/open/claimed/blocked 카운트 (→ `board.py list`) · open ticket ID 목록 (→ `pm_bootstrap`) · commit 해시·push 상태 (→ `git log`/`git status`) · 직전 complete entry 산출물 재요약 (→ 자동 박제 목록의 헤더와 인접 log 원문이 답한다). 회귀 1줄 baseline 은 예외다.
2. **domain capture (채록) 검토** — `python3 .project_manager/tools/domain.py capture --tickets "T-0001,T-0002"`(이 세션 done ticket ID — 콤마분리 또는 공백 나열 `T-0001 T-0002`) 실행. 출력의 *영향 페이지*(`⚠ `=stale) 와 *coverage gap*(담당 페이지 없는 touched 경로)을 보고 관련 domain 페이지를 갱신하거나 신규 scaffold 한다. **갱신 = 현재-진실 교체** — 세션별 delta 를 페이지에 덧붙이지 마라("언제 왜 바뀌었나"는 log/ADR 몫·domain lint `history` 축이 검출). **surface-only** — 도구는 *무엇을 갱신/신설할지 띄울 뿐*, 본문 자동생성·`updated:` 자동스탬프는 안 한다(stale 탐지 거짓 방지). 갱신할 것 없으면 생략.
3. **git commit — pathspec 필수** — 공유 PM 홈에서 bare `git commit` 은 다른 슬롯 WIP도 싣는다. **이번 세션이 만든 것을 전부, 그리고 그것만** 나열한다:

   ```bash
   git add .project_manager/wiki/domain/<신설한 페이지>.md          # 신규 파일은 add 선행 필수
   git commit -m "PM 세션(N차) 핸드오프 — pm_state.md sliding window + log/current.md handoff entry + PM (N+1)차 인계" -- \
     .project_manager/wiki/log/current.md \
     .project_manager/wiki/domain/<위 2단계에서 갱신/신설한 페이지>.md \
     .project_manager/wiki/status.md .project_manager/wiki/status_done.md
   ```

   - **`log/current.md` 하나만 적으면 위 2단계 domain capture 산출을 잃는다.** 이 CLI 가 스스로 쓰는 파일은 `log/current.md` 뿐이지만 잔여 손작업 1~2 단계에서 고친 domain 페이지·status 도 이번 세션 산출이다. 고치지 않은 줄은 지운다.
   - 핸드오프엔 `$pm-wave-finish` 같은 스코프 잔여 보고가 없다. [6/7] `git status -s` dump에서 *내 세션 산출*을 골라 pathspec 에 넣고 남의 WIP 는 남긴다.
   - **신규 파일은 `git add` 선행 필수** — 미추적 경로를 pathspec 에 주면 `error: pathspec '…' did not match any file(s) known to git` 으로 **커밋 전체가 rc=1 로 죽는다**(실측).
   - `pm_state.md`(solo `wiki/pm_state.md` · per-slot/per-task `.local/…`)는 **gitignored** 라 커밋 대상이 아니다. commit message 에만 남긴다. trailer `Co-Authored-By: Claude`.
4. **마지막 응답에 인계 프롬프트(트리거) 코드블록 출력** — 다음 세션은 `$pm-bootstrap` 실행(트리거 붙여넣기 or 직접). 인계 본문은 부트스트랩이 log entry 에서 자동 dump 하므로 손-채움 불요.

참고: `.project_manager/tools/pm_handoff.py`(backbone CLI), `.project_manager/wiki/pm_role.md`(핸드오프 절차 7단계 단일 진실).
