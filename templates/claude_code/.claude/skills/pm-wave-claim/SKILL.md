---
name: pm-wave-claim
description: "wave 안 ticket claim — board show + DoD self-containment PM 검증 + claim. ticket 본문에 placeholder / depends_on 미충족 / wikilink dangling 있으면 차단. Triggers: 'T-NNNN claim', 'ticket 잡기', 'wave 시작', 'pm-wave-claim'."
audience: pm-internal
---

# /pm-wave-claim T-NNNN — wave 시작 ticket claim

wave 시작 시 ticket 하나를 claim하며, claim 전에 ticket self-containment를 PM이 검증한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 실행

```bash
# 1. ticket 본문 dump
python3 .project_manager/tools/board.py show T-NNNN

# 2. lint (의존성 일관성)
python3 .project_manager/tools/board.py lint

# 3. PM 검증 (아래 체크리스트)

# 4. 통과 시 claim
python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 솔로(M=1)면 생략 가능
```

## claim 전 PM 검증

전부 충족할 때만 claim한다. 실패하면 ticket 본문부터 보강한다(pm_playbook.md §메타 정책 "Ticket 본문").

- [ ] **표준 섹션** — 목표 / 인터페이스 / 결정 / 설계 / 완료 조건 / 참고 / 메모(7절). lint는 **목표·완료 조건·참고 3개만** 누락 차단(`_REQUIRED_SECTIONS`)하므로 인터페이스·결정·메모는 PM이 채운다. `## 설계`는 `design: required` 티켓만 채운다(아래 항목). `<...>` placeholder도 0개여야 한다(lint 차단).
- [ ] **설계 단계 해소** — frontmatter `design:`이 `done` / `"waived: <사유>"` / `n/a`(필드 부재 포함) 중 하나여야 한다. `required`로 남아 있거나 `## 설계` 절이 뼈대(경계 실측·불변식·표면 상한·테스트 전략)면 **claim이 rc=1로 거부**된다. 설계 절을 완성하고 설계 검토(리뷰어 세션 겸임·상한 2라운드)를 마친 뒤 PM이 `design: done`을 수동 기입한다. 작성 규칙은 `/pm-ticket` §설계 단계.
- [ ] **depends_on 모두 done** — open/claimed 의존이면 차단. blocked 의존은 reason 확인.
- [ ] **touches 명시** — wave 병렬의 touches disjoint 안전성 검증에 필요. 누락 시 보강.
- [ ] **wikilink dangling 0개** — `[[name]]`이 실제 페이지·메모리·ADR·ticket을 가리키는지 lint 또는 별도 회귀 가드로 확인.
- [ ] **DoD verify-able** — 충족 evidence 측정 방법(테스트, 단위 수, 라이브 검증 절차, spec 정합 확인 등)이 본문에 명시.
- [ ] **컨텍스트 예산** — 대형 파일/광범위 읽기가 필요하면 분할하거나 정확한 함수/라인·패턴 reference를 본문에 넣어 cold dev의 읽기 범위를 줄인다.
- [ ] **PM 자율 vs 사용자 게이트** — 보호 영역 / mission scope / 외부 비가역 행위 영향 시 사용자 게이트 통과 확인(pm_role.md §사용자 게이트).
  - 보호 영역: `.project_manager/wiki/pm_role.local.md` §보호 영역

## claim 분기

PM 자율 claim 가능(pm_role.md §"자율 + 사후 로그"):

- `scope: internal-process` ADR 산출 ticket
- 핵심 안전 경계·자본·외부 비가역 무영향·가역
- 사용자 게이트 항목 무관

사용자 게이트 후보는 **claim 보류 + 사용자 결정 대기**.

실패 시:

- **본문 부족** → PM이 cold dev가 본문만 보고 시작 가능하도록 직접 보강.
- **depends_on 미충족** → 의존 ticket 우선 진행 또는 본 ticket blocked 처리.
- **lint warning** → 의존성 모순 fix.
- **wikilink dangling** → log/current.md 메타 entry 인용을 link 형태 또는 raw 단어로 재작성.
- **design 미해소** → claim이 `cannot claim <T-NNNN>: 설계 단계 미완 — …`로 rc=1 거부한다(`design-pending` 라벨은 lint·promote 출력에만 붙는다). `## 설계` 절을 채우고 설계 검토 후 `design: done` 기입. 설계가 불필요하면 `design: "waived: <사유>"`로 사유를 남긴다(콜론이 들어가므로 YAML 따옴표 필수).

## 제약

- board.py lint 자동 검증은 placeholder·표준 섹션 중 목표/완료 조건/참고·순환 의존이다. `design: required` 미해소는 lint에서 `design-pending` 경고 1줄(never-block)이고 실제 차단은 claim/promote가 한다. DoD verify-able·self-containment·게이트 분류는 PM이 수동 검증한다.
- wave 안 **1 ticket 1 claim**. 동시 다중 claim 금지.
- dev/reviewer 위임 ticket도 PM이 claim한다(board.py claim은 orchestrator/PM 영역). 서브에이전트는 구현/검토만 한다.
- ticket 본문 self-contained 의무와 claim 워크플로의 단일 진실은 `.project_manager/wiki/pm_role.md`.
