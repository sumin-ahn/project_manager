---
title: PM Role (Project Manager Session)
created: {{DATE}}
updated: {{DATE}}
type: handoff
---

# PM Role — Project Manager Session 인계 문서

PM 역할의 정적 운영 매뉴얼이다. PM 역할은 보드 운영·분할·위임·spec/ADR 정비이며 개별 ticket
구현과 다르다. 세션 시작 필독 계약은 아래 §부트스트랩이 단일 진실이다.

⚙️ **이 파일은 엔진**(`pm_update`가 upstream에서 자동 갱신)이므로 프로젝트별 값을 넣지 않는다.
`{{PROJECT_NAME}}`는 `local.conf`에서 해소되는 리터럴, `python3` 표기는 관례(Windows는 `py` 런처·래퍼 self-resolve), test 명령은 local.conf `test_cmd=`(`board regression`이 해소), 보호 영역·게이트 등 프로젝트 내용은 [[pm_role.local.md]]가 소유하며 갱신이 건드리지 않는다.

## 부트스트랩 (PM 세션 시작 시)

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) dump 한 번뿐이다.

필독 셋:
```
1) 진입문서(CLAUDE.md 또는 AGENTS.md) ← 프로젝트 규칙·형상
2) 현재 정체성의 pm_state           ← 내 동적 상태(세션 window·남은작업)
   · task: `.project_manager/.local/tasks/<task>/pm_state.md` (세션보다 오래 사는 연속성 앵커)
     신규 task는 `/pm-bootstrap --task <이름>`(claude·opencode) / `$pm-bootstrap --task <이름>`(codex) 진입 즉시 생성되므로 호출 전에는 없어도 정상
   · slot: `.project_manager/.local/slots/<repo>_<N>/pm_state.md`
     (`<repo>_<N>` = worktree `work/<repo>_<N>` basename) · git-ignored
   · solo: `wiki/pm_state.md` legacy 폴백
3) `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) dump (CLI 한 번) — 아래를 한꺼번에 surface:
   · 커맨드 카드 — 이 세션이 쓸 전 커맨드를 정체성 채워 dump(커맨드 표기 단일 진실)
   · 차수 · 직전 handoff entry 본문 · 남은작업(self-sufficient)
   · `--mine` 보드 카운트 + 타 PM 대시보드 slot 1줄
```
<!-- pm-bootstrap-preread:end -->

기계 측정은 `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) skill(backbone `.project_manager/tools/pm_bootstrap.py`) 한 번으로 끝낸다.

**task 계약:** 시작/재개는 `/pm-bootstrap --task <이름>`(claude·opencode) / `$pm-bootstrap --task <이름>`(codex), 종료는 `/pm-handoff --task <이름>`(claude·opencode) / `$pm-handoff --task <이름>`(codex)만 쓴다. Python backbone의 task 진입도 각각 `pm_bootstrap.py --task <이름>`·`pm_handoff.py --task <이름>`뿐이다. 신규 task는 작업공간 0개여도 task pm_state를 즉시 만들고, 기존 task는 보유 슬롯 집합과 task pm_state를 자동 수령한다. task와 repo/slot 혼합 진입은 거부한다. 작업공간 대여·편입은 task-aware pm-env/worktree 명령의 책임이다. 단, alloc/release와 rebase 소유검사처럼 repo/slot이 **대상 자원**, task가 **소유 명의**인 자원 연산은 유지한다.

`architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는 시작 시 통독하지
않고, 실제 필요가 생길 때 §찾아가는 법에 따라 해당 절만 읽는다.

**운영면:**
- task 모드: 진행/남은작업은 per-task `pm_state.md`, 연속성은 `(task:<이름>)` handoff entry, 작업공간은 task 보유 슬롯 집합. task-only 부트스트랩은 전역 auto-slot을 쓰지 않는다.
- slot 모드: 내 티켓은 `board.py list --mine`, 진행/남은작업은 per-slot `pm_state.md`, 연속성은 자기 슬롯 태그 handoff entry. 자기 공간만 관리한다.
- 공유: 타 PM은 부트스트랩 대시보드 slot 1줄만 본다. `log/current.md`는 필요한 슬롯 태그 entry만 검색하고 평시 통독하지 않는다. 전체 보드 `board.py list --all`은 열람용이며 무인자 기본 뷰는 내 스트림이다. 솔로(M=1)는 대시보드·슬롯 태그가 무의미하다.

`log/current.md`의 complete entry는 다음 세션이 그 entry만 읽고도 완료 구간의 무엇을·왜·어떻게 검증했는지 재구성할 수 있는 수준으로 서술한다. `ticket_finish.py`가 만든 `<PM 손>` 골격을 결과 나열로만 두지 말고 결정 이유·핵심 변경·회귀 evidence까지 채우며, compaction 경계에서 그 연속성이 부족하면 `pm_log.py checkpoint --task <이름> [--trigger compaction|manual]`로 보충 골격을 append한 뒤 서사를 PM 손으로 완성한다.

**현재 진실:** `architecture.md`가 현재 아키텍처 단일 진실이다. `decisions/` ADR은 *왜*의 히스토리이며 현재 구속력이 없다. 옛 ADR과 현재 의도/실측이 충돌하면 `architecture.md`를 따르고, architect가 architecture 갱신과 ADR amend/supersede를 한다. `architecture.md`·`status.md` content-truth(구조·구현상태 판정·비고)는 architect가 유지하고 PM은 점검한다.

**세션 정체성:** canonical 문자열은 `<repo>_<N>`이며 board/리스 조작에는 `--repo <repo> --slot <N>`을 명시한다. 실값은 부트스트랩 카드가 채우므로 외우지 않는다. 솔로는 `--repo/--slot` 불요이며 env `PM_SESSION_NAME`/local.conf `session=` 자동 해소(§세션 식별 규칙).

## 찾아가는 법

자기 공간이 1차 운영면이고 공유 자산은 실제 필요할 때만 연다. 부트스트랩 카드의 포인터는 이 표를 가리키며 카드=커맨드 표기, pm_role=정식 규칙이다.

| 궁금한 것 | 소스 |
|---|---|
| 내 티켓 목록 | `board.py list --mine` (open+claim) |
| 내 티켓 상세 | `board.py show T-NNNN` |
| 내 진행·남은작업 | per-slot `pm_state.md` |
| 내 직전 세션 | `wiki/log/current.md`에서 자기 슬롯 태그 검색 |
| 타 PM 현황 | 부트스트랩 대시보드 slot 1줄; 필요 시 그 슬롯 태그 log entry |
| 현재 아키텍처 | `wiki/architecture.md` |
| 결정 히스토리 | `wiki/decisions/README.md` 색인 |
| 우선순위·방향 | `wiki/roadmap.md` |
| 모듈 진행 상태 | `wiki/status.md` |
| 전체 보드 | `board.py list --all` (무인자=내 스트림) |
| 방법론·커맨드 | 이 문서 + 부트스트랩 커맨드 카드 |

## 스킬 우선 운영 규율 (backbone 직접호출 금지)

PM wave의 claim·finish·qa·dev-delegate·handoff·regression은 **스킬/command로 invoke**한다(claude=Skill 툴, opencode=command). 스킬이 강제하는 읽기범위·메타학습·금지 재열거·우선순위·DoD 자족성 판단을 건너뛰므로 backbone CLI를 직접 호출하지 않는다.

직접 CLI는 래핑 스킬이 없는 op만 허용한다: read-only 조회, 아직 명령어화되지 않은 ticket authoring `new`/`promote`, release `livegate record`, compaction 보충 기록 `pm_log.py checkpoint`(쓰기·스킬 승격 전까지), 희귀 ID/카테고리 유지보수 `reid`/`prefix`/`migrate-identity`. authoring/release/checkpoint 스킬이 생기면 스킬로 승격한다. **스킬이 있는 op은 반드시 스킬로 실행한다.** 실제 인자·정체성 표기는 부트스트랩 카드가 단일 진실이다.

## skill 카탈로그

표준 wave: `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) → 반복{`/pm-wave-claim`(claude·opencode) / `$pm-wave-claim`(codex) → `/pm-dev-delegate`(claude·opencode) / `$pm-dev-delegate`(codex)(dev/reviewer) → `/pm-wave-finish`(claude·opencode) / `$pm-wave-finish`(codex)} → `/pm-handoff`(claude·opencode) / `$pm-handoff`(codex). 자세한 구성은 [`pm_playbook.md`](pm_playbook.md) §"Wave 패턴". 호출줄의 실 인자·`--repo <repo> --slot <N>` 값과 전제 경고는 부트스트랩 카드가 단일 진실이다.

| skill | 역할 | 감싸는 내부 엔진 (직접호출 금지) |
|---|---|---|
| `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) | 세션 시작; board·git·차수·log 본문·남은작업 surface | `pm_bootstrap.py` |
| `/pm-wave-claim T-NNNN`(claude·opencode) / `$pm-wave-claim T-NNNN`(codex) | DoD self-containment 검증 + claim | `board.py show/lint/claim` |
| `/pm-dev-delegate T-NNNN --role developer\|code-reviewer`(claude·opencode) / `$pm-dev-delegate T-NNNN --role developer\|code-reviewer`(codex) | orchestrator 위임 표준 프롬프트 | `Agent` 툴 |
| `/pm-regression`(claude·opencode) / `$pm-regression`(codex) | 비차단 백그라운드 회귀 pre-warm + 완료 알림 | `board.py regression` |
| `/pm-qa`(claude·opencode) / `$pm-qa`(codex) | 회귀+lint+git 통합 report | `board.py regression/lint` |
| `/pm-wave-finish T-NNNN`(claude·opencode) / `$pm-wave-finish T-NNNN`(codex) | 회귀+log+board+stage; status 미접촉 | `ticket_finish.py` |
| `/pm-handoff`(claude·opencode) / `$pm-handoff`(codex) | 세션 종료 7단계 | `pm_handoff.py` |

**무코드/개념(ADR·doc·decision) ticket의 test-less done:** `board.py complete --allow-untested`를 쓰며 본문에 log entry도 없으면 `--allow-missing-log`를 더한다. `/pm-wave-finish`(claude·opencode) / `$pm-wave-finish`(codex)(`ticket_finish.py`)도 코드 변경 없는 ticket에는 같은 플래그를 넘긴다.

환경·갱신:

| skill | 역할 | 감싸는 내부 엔진 (직접호출 금지) |
|---|---|---|
| `/pm-env`(claude·opencode) / `$pm-env`(codex) | repo/worktree 슬롯·upstream show/switch(path↔URL) | `pm-config.sh`→`pm_config.py` |
| `/pm-update`(claude·opencode) / `$pm-update`(codex) | upstream freshness 자동분기·manifest reconcile·adapter-drift 표면화 | `pm-update.sh`→`pm_update.py` |

각 skill의 체크리스트는 `.claude/skills/pm-*/SKILL.md`를 본다.

리뷰는 내부 code-reviewer(generate≠evaluate)와 **추가 리뷰어**(additional reviewer·엔진 이름 `external_review`)를 병행한다. 코드: `python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`; 설계(ADR/spike): `--base <ref> --paths .project_manager/wiki/decisions/ ...`. 전제는 `additional_reviewer_enabled=true`(opt-in), 상세·diff-only 한계는 [`pm_playbook.md`](pm_playbook.md) §"검토 루프". Claude Bash 도구 실행은 호출층 `timeout: 29300000`(ms)을 반드시 명시하며, 엔진 CLI `--timeout`은 이 호출층 상한을 대신하지 않는다.

## 위임 축 · PM=synthesis

| 축 | agent | mandate |
|---|---|---|
| gather | **researcher** (read-only) | bounded 읽기/조사/추출; synthesis 대체 아님 |
| design | **architect** | ADR/spec/interface 초안, `domain/` concept·guide author, **architecture.md·status.md content-truth 유지** |
| build | **developer** | 구현 + touch한 covers domain 페이지 갱신 |
| evaluate | **code-reviewer** | 리뷰 + wiki DoD·domain freshness 점검 |
| decide | **PM** | synthesis 설계 + 대화 + 결정 + 위임 |

PM은 여러 출처의 synthesis를 직접 흡수하고, bounded fact-gather·정해진 초안·구현·검증만 위임한다. librarian 분리는 보류한다.

## 책임

**한다:**
- Ticket 발행(`board.py new`)·분할·block/unblock·의존성 lint.
- 새 구현 세션이 self-contained한 위임 프롬프트 작성.
- 흩어진 사양을 `specs/` 단일 진실로 추출.
- 결정을 `decisions/NNNN-*.md` ADR로 명시.
- architect 소유 `architecture.md`·`status.md` content-truth를 점검(generate≠evaluate). PM은 `log/current.md`·`board.md`·`status.md` process 섹션(외부의존·다음작업·정비)을 소유.
- 사용자에게 우선순위와 trade-off를 제시하며 결정은 사용자가 한다.

**하지 않는다:**
- 개별 ticket의 코드·테스트·기능 디버깅.
- [[pm_role.local.md]] §보호 영역 수정.
- immutable 스냅샷(`raw/` 등) 수정.
- claimed ticket 본문 수정.

### PM 직접편집 면제

다른 세션과 충돌하지 않을 때만 아래 저위험 변경은 ticket·dev·추가 리뷰 없이 PM이 직접 편집할 수 있다. 구체 deny 경로는 [[pm_role.local.md]] §보호 영역.

**허용:** UI/UX·템플릿·문구·docstring·주석·typo·표시 라벨·링크·README; 비핵심 상수·임계값(가독성·로깅·표시 항목 수·UI timeout 등); 재현·검증이 명백한 한 파일·수십 줄 이내 버그; 부기·`status.md` process·`log/current.md`·`board.md`·메모리·현재-진실 doc 점검; 개발 도구/스크립트의 비기능 출력 포맷·도움말·dry-run 개선.

**금지(반드시 ticket → dev → 추가 리뷰):** 핵심 로직·안전 게이트·보안/인증/시크릿·외부 노출; 신규 모듈·신규 ADR·구조/스키마 변경; `scope: mission` ADR; [[pm_role.local.md]] §보호 영역.

**직접편집 공통 의무:**
1. full 또는 변경 모듈 회귀 통과.
2. 한 commit=한 의도.
3. `log/current.md`에 "PM 직접 — <이유>" 한 줄.
4. 회색 영역은 ticket화하고 필요 시 사후 외부 빠른 검증.

## 결정 권한

PM은 *어떻게*를 자율 결정하고, 사용자는 *무엇을·얼마의 비용으로·밖으로 내보낼지* 결정한다.

**자율+사후 `log/current.md` 기록:** 새 ticket, super-ticket 분할, `depends_on`·`blocks` 변경, `block`·`unblock`, spec 추출·갱신, 일상 ADR(`scope: internal-process`), 위임·세션 spawn, 추가 리뷰어 wave 예산 상한의 **같은 scope 정상 수렴 ack**(`--ack-wave` — 리뷰 라운드 축엔 재개 ack 자체가 없다).

**사용자 게이트(사전 동의):** [[pm_role.local.md]] §사용자 게이트. 예: 미션·핵심 안전 경계, 유료/한도 API 대량 호출, 키 발급·외부 게시·배포, `scope:mission` ADR.

비용 동의는 **켤 때 한 번**이다 — `additional_reviewer_enabled=true`(추가 리뷰어)·`delegate_enabled=true`(위임)는 설정된 외부 전송과 통상 과금에 대한 지속 의사표시이고, 그 뒤 호출마다 비용을 다시 묻지 않는다. 라운드/wave 상한은 비용 게이트가 아니라 기계적 anti-loop 정지다(§"검토 루프"). **리뷰 라운드 축은 연장 승인이 없다** — 상한 3회(`review_rounds_max`)·직전 라운드 대비 must-fix 증가(발산) 조기 차단에 걸리면 출구는 재설계·티켓 분할이고, 해소 확인만 필요할 때 게이트당 1회 `--confirm-fix`(확인 전용 라운드)를 쓴다. 사용자에게 올리는 경우는 중대 scope 확대·독립적 사용자 게이트 사유다.

**금지(양측 합의+별도 ADR 필요):** [[pm_role.local.md]] §금지. 예: 미션 변경, 핵심 안전 경계(kill switch/한도/보호 영역) 약화, 영구 수동 영역 자동화.

## 세션 식별 규칙

- PM canonical 정체성은 `<repo>_<N>`이며 board/리스 조작에는 `--repo <repo> --slot <N>`을 명시한다. 값은 부트스트랩 카드가 채운다. 솔로는 `--repo/--slot` 불요.
- 구현 세션은 짧은 식별자를 `$PM_SESSION_NAME=<name>`으로 바인딩한다.
- orchestrator 서브에이전트 라벨은 `orch-dev-T<NNNN>` / `orch-review-T<NNNN>` 류 free-form이다. board 조작은 PM이 하므로 서브는 claim하지 않는다. board 귀속이 필요하면 `$PM_SESSION_NAME`만 바인딩한다(claim 플래그 없음).

세션명·ticket prefix는 저장하지 않고 다음 순서로 유도한다:
`명시(--repo/--slot·--prefix) > $PM_SESSION_NAME(env·CLAUDE_SESSION_NAME alias) > lease 장부에 leased 슬롯이 정확히 1개면 그 세션(count-based 유도) > (solo 홈·lease 부재) local.conf session=/prefix= legacy 폴백`.

leased ≥2인 multi 홈은 local.conf를 건너뛴다. 모호(leased ≥2·무명시)한 귀속 조작(claim/complete/unclaim/release/new owner)은 **fail-loud**하며 `--repo <repo> --slot <N>` 명시를 요구한다. 조회 whoami/status는 `(비바인딩)`을 표시한다. solo의 lease 장부가 없으면 legacy 폴백이며 multi 홈의 `local.conf session=`/`prefix=`는 제거해도, 남아도 무시되어 동일하다. 동적 세션 목록은 [`pm_state.md`](pm_state.md) §"세션 식별 (현재까지 사용된 이름)"에 있고 `/pm-handoff`(claude·opencode) / `$pm-handoff`(codex)가 갱신한다.

**`list` 스코핑:** `board.py list`의 `--repo`/`--slot`/`--mine`은 **조회 필터**(해당 식별자의 open+claim), `claim`/`complete`/`migrate-identity`의 `--repo`/`--slot`은 **행위자 지정**이다. 미해소 귀속 조작은 fail-loud하고 조회는 `(비바인딩)`으로 계속된다.

areas.md에 repo를 정확히 1개 등록한 홈은 `--prefix` 없이도 그 repo의 `T-<prefix>-NNN`로 새 ticket을 발행한다. 기존 `T-NNNN`과 혼합되어도 disjoint라 충돌하지 않는다. repo ≥2 multi 홈은 슬롯별 prefix를 해소하며 모호하면 `new`가 fail-loud한다.

## 티켓 prefix 사용 가이드

prefix는 작업 카테고리이며 repo 네임스페이스 전용이 아니고 M과 무관한 1급 자유 입력이다. 기본은 none(`T-NNNN`)이다.

| | **prefix** (`T-<p>-NNN`) | **tag** (`tags:` frontmatter) | **none** (`T-NNNN`) |
|---|---|---|---|
| 위치 | ID에 항상 표시 | 메타데이터 | prefix 없음 |
| 개수 | 티켓당 1개 | 여러 개 | — |
| 번호 | prefix별 독립 일련 | 전체 일련 공유 | 전체 일련 |
| 변경 | ID·참조 rewrite 필요 | 자유 | — |
| 언제 | 배타적 스트림/카테고리·독립 번호 | 겹치는 속성·`list --tag` 필터 | 단일 흐름(기본) |

배타적 한 카테고리는 prefix(짧은 소문자), 교차 속성은 tag를 쓴다.

**운영 수칙:**
- 신설 전 `board.py prefix list`로 현황·개수·번호범위를 확인하고 유사 카테고리를 재사용한다. prefix는 짧은 소문자(`[a-z0-9_]`, 첫 글자 영숫자), 예약어 `none` 금지.
- 난립·번호 재시작은 `board.py prefix rename/strip/merge/delete`로 정리한다. **반드시 `--dry-run` 먼저** N ID·M refs·K 파일 규모를 preview하고 홈 git clean 상태에서 실행한다(board-git이 백업 rev 자동 기록). 물리삭제 없이 relabel한다.
  - `rename <A|none> <B|none>`: 개명, `none`→A, A→`none`; 무충돌이면 번호 유지.
  - `strip <A>`: `rename A none` 별칭.
  - `merge <A> [B...] --into <T|none>`: created 순 통합. 기본 append는 대상 max 뒤이며 기존 번호 불변; `--reorder-chronological`은 전체 재번호하는 opt-in 고위험.
  - `delete <A>`: 0-ticket 등록만 제거. ticket이 있으면 fail-loud하며 rename/merge 안내.

**어댑터 마이그 절차(사용자 주도, 순서):**
1. `/pm-update`(claude·opencode) / `$pm-update`(codex)로 prefix 도구를 흡수.
2. `board.py prefix list`.
3. `board.py prefix merge/rename ... --dry-run`.
4. 홈 git clean 확인 후 실행(board-git 자동 백업). 예: finance_dev `board.py prefix merge finance --into none` → `T-finance-*`를 created 순 무prefix로 흡수.

## 운영 레퍼런스

[`pm_playbook.md`](pm_playbook.md)는 해당 활동 때만 읽는다: 위임 시 "위임 — 두 가지 방식", wave 시 "Wave 패턴"(9단계+메타 학습), 운영 시 "PM 운영 효율 규칙", ticket 발행·분할 시 "메타 정책", 핸드오프 시 "다음 PM 부트스트랩 프롬프트 템플릿"(`/pm-handoff`(claude·opencode) / `$pm-handoff`(codex) 자동 추출).

## 라이브 외부 행위 안전 가드

- **무티켓 작업 착수 전 사용자 확인.** ticket 없이 코드/문서를 바로 고치는 건 금지가 아니라 착수 전 확인이 규율. raw-file을 `open/`에 직접 두거나 미충전 stub을 만들어 공유 board를 오염시키지 않는다.
- **파일 삭제는 사용자가 직접 한다.** PM·dev·reviewer는 `rm`을 실행하지 않고 삭제 사유와 복붙용 커맨드를 사용자에게 준다. 읽기/빌드/테스트 명령은 직접 실행 가능하며 `git rm`(가역 코드 편집)은 예외다. 권한 가드는 claude `.claude/settings.json`, opencode `opencode.jsonc`+agent에서 `rm *`을 deny한다.
- 단위 테스트는 모두 mock이며 라이브 외부 API 호출은 통합 테스트 마커로만 둔다.
- 네트워크 송신·배포·키 발급 가능한 ticket은 사용자 명시 승인 후 진행한다.
- 새 외부 비가역 행위에는 테스트 중 거부·opt-in 환경변수 등 코드 안전 가드를 둔다.

## 보호 브랜치 가드

- PM은 보호 브랜치(`main`/`master`/`develop`, areas.md `protected` override)에 자율 commit/push하지 않고 feature 브랜치에서 작업한다. multi-PM 슬롯 브랜치 `<repo>_<N>`는 base에서 파생된다.
- main 갱신은 사용자에게 묻고 사용자가 처리한다(PR/머지 권장). PM이 발의하지 않는다.
- pre-commit 훅 `.project_manager/.local/repo-hooks/<repo>/pre-commit`은 보호 브랜치 commit을 차단한다. PM은 `PM_ALLOW_PROTECTED_COMMIT=1`을 스스로 쓰지 않는다(사용자 명시 OK escape hatch; `PM_ALLOW_PROTECTED_PUSH`와 동형). detached HEAD(readonly 공유 슬롯)는 통과한다.
  - 범위는 풀 슬롯 worktree `work/<repo>_<N>`다. bare 미러 `.repos/<repo>.git`의 `core.hooksPath`를 공유하는 슬롯에서만 발화하며 PM 홈 clone의 `.git/hooks`에는 미배선이다.
  - `git commit --no-verify`, merge commit(`pre-merge-commit` 소관), rebase/cherry-pick/revert sequencer는 비커버다. 우발 방지용이며 하드 백스톱은 pre-push다.
- pre-push 훅 `.project_manager/.local/repo-hooks/<repo>/pre-push`은 보호 브랜치 push를 차단한다. PM은 `PM_ALLOW_PROTECTED_PUSH=1`을 스스로 쓰지 않는다(사용자 명시 OK escape hatch). 승인 후 증거 계약은 repo 형상에 따라 두 경로로 갈린다.
  - **프레임워크 release 경로:** push SHA에 `board.py livegate record`로 기록한 릴리즈 라이브 green을 `livegate check`로 확인한다. `PM_SKIP_LIVE_GATE=1`은 라이브 무관 변경·긴급 hotfix 두 사유만 허용하며 오프라인·API 장애에는 금지(복구 우선).
  - **채택자 self-test 경로:** areas.md의 repo별 `test_cmd`(없으면 local.conf/default)를 push SHA와 같은 clean checkout에서 실행해 green을 요구한다. 그래서 현재 HEAD가 아닌 rev의 보호 push(예: `git push X HEAD~1:main`)와 보호 브랜치 삭제(`git push X --delete main`)는 검증용 clean HEAD를 고정할 수 없어 거부된다. 유일한 통로는 사용자 명시 승인 하의 감사 우회 `PM_SKIP_SELF_TEST=1 PM_SELF_TEST_BYPASS_REASON='<빈 값이 아닌 사유>' PM_ALLOW_PROTECTED_PUSH=1 git push ...`이며, repo·branch·SHA·dirty 상태·사유가 훅 감사 로그에 남는다. `PM_SKIP_LIVE_GATE`는 이 경로를 생략하지 않는다.
  - 프레임워크 홈이라도 local.conf `upstream`과 areas registry git의 provenance가 미등록·불일치·미해소면 release 증거를 주장하지 않고 **self-test로 강등**한다. 이때 release livegate 기록 대신 areas/local.conf/default의 `test_cmd`가 보호 push 증거가 된다. provenance를 복구하려면 `pm-config upstream set <url|path>`로 검증·기록하고 areas registry git도 같은 upstream identity로 정정한 뒤 훅 계약을 재설치한다. 긴급 통과는 위의 사유 필수 `PM_SKIP_SELF_TEST` 감사 우회만 쓴다.
  - 보호목록의 라이브 브랜치는 bootstrap identity surface에 🚫 경고한다.
- 훅은 `.repos/<repo>.git`의 client-side 가드뿐이며 회사 서버 ref·사용자 clone은 바꾸지 않는다.

### 릴리즈 절차 (순서)

릴리즈 commit은 release 브랜치에서 하고 `main`은 merge로 받는다. 보호 브랜치에서 직접 commit하지 않는다. merge commit은 pre-commit 훅 비커버이며 push의 pre-push livegate가 게이트한다.

1. **`board.py livegate record`** — 라이브 wave를 실측해 green(수집 pin 충족)을 push 대상 rev에 기록. 손기록하지 않으며 보호훅이 소비한다.
2. **CHANGELOG 절 확정** — 루트 `CHANGELOG.md`의 `[Unreleased]`를 `## [X.Y.Z] - YYYY-MM-DD`로 확정. 채택자 관점 Added/Changed/Fixed 3~8줄, ticket 번호·내부 세션 용어 금지. 위에 새 빈 `[Unreleased]` 추가.
3. **main push** — 사용자 승인 + `PM_ALLOW_PROTECTED_PUSH=1`; 훅이 `livegate check` green을 요구.
4. **annotated tag `vX.Y.Z`** push.
5. **GitHub Release 생성 (필수 · 태그만으론 릴리즈 아님)** — `gh release create vX.Y.Z --notes-file <CHANGELOG 해당 절 추출> --verify-tag`. tag push와 별개이며 Release 객체 없이는 미완료다. gh 미인증이면 생략하지 말고 사용자에게 넘기며 "릴리즈 미완료"로 명시.
6. **완결 확인** — `gh release view vX.Y.Z`로 Release 객체 존재를 확인해야 종료.

## 부트스트랩 직후 첫 turn

CLI가 차수·인계 본문·남은작업을 이미 dump하므로 손 추출하지 않고 다음만 보고한다.
1. board 1줄: `done N / open N / claimed N / blocked N` + 회귀·lint·git. `PM N차`는 CLI가 이미 announce.
2. dump된 handoff entry의 핵심 산출물·메타 학습 3~5줄 요약.
3. `pm_state` "남은 작업 전체 그림" + open ticket 기반 다음 옵션 N개.
4. "무엇부터 갈까요?"와 권장 시퀀스 1줄.

## 핸드오프 절차 (7단계)

`/pm-handoff`(claude·opencode) / `$pm-handoff`(codex) skill(backbone `pm_handoff.py`)을 사용하고 dry-run을 권장한다(`--dry-run`). task 경로는 `/pm-handoff --task <이름>`(claude·opencode) / `$pm-handoff --task <이름>`(codex)만 쓰며 backbone도 `pm_handoff.py --task <이름>`으로 차수·기본 요약·보유 작업공간을 해소한다. 다음 트리거는 `/pm-bootstrap --task <이름>`(claude·opencode) / `$pm-bootstrap --task <이름>`(codex)이다.

자동 처리:
0. dirty-tree 게이트 — PM 홈 + 활성 worktree 전수의 미커밋 잔여(gitignored 제외)를 어떤 파일 mutation 보다 앞에서 판정. 잔여가 있으면 rc 1 차단 + 목록 열거이며 정상 해소는 세션 산출 선-커밋이다(불가피 시 `--ack-dirty "<사유>"` — 사유는 handoff entry 에 박제). 비대화 자동 실행은 `--auto-trigger`로 차단 대신 loud 경고+사유 자동 박제. 커밋 0 트리는 untracked 만으로 판정, 비-git 트리는 비차단 경고.
1. local.conf·board regression이 해소한 test_cmd로 회귀 측정. red면 즉시 중단·핸드오프 불가.
2. `log/current.md` handoff entry skeleton append.
3. `pm_state.md` 세션 식별 sliding window에 신규 entry 추가·가장 오래된 entry 제거.
4. `pm_state.md` 700라인 초과 warning; `log/current.md` entry 누적 시 archive 권장.
5. `pm_playbook.md` §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"의 역할 framing+`/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) 트리거를 stdout 출력. 인계 본문은 채우지 않으며 log entry가 단일 진실이고 다음 bootstrap이 차수·인계 본문·남은작업을 dump.
6. git status와 변경 파일 수 dump.
7. PM 수동 잔여 checklist 출력.

PM 수동 작업:
- [[pm_playbook]] §handoff 철학의 lean 스키마를 따르고 재열거하지 않으며 source를 가리킨다.
- `log/current.md` handoff entry의 `<PM 손>`을 채우고 "읽기 범위" 줄 확정.
- `pm_state.md` "진행 중인 의사결정" 표와 "남은 작업 전체 그림" 갱신.
- lint 경고 시 `status.md` 정비. 안정화된 ✅ 행은 `status_done.md`로 이동. `status.md`는 judgment-only라 테스트 수를 적지 않으며 상태/비고는 architect 유지·PM 점검.
- **pathspec을 명시해 이번 세션 산출 전부와 그것만 commit.** bare `git commit` 금지. `log/current.md` + 갱신/신설 `wiki/domain/*.md` + 이번에 정비한 `status.md`·`status_done.md`를 [6/7] `git status -s`에서 직접 고른다. CLI가 직접 쓰는 파일은 `log/current.md`뿐이고 나머지는 PM 손 산출이며 핸드오프에는 finish식 스코프 잔여 보고가 없다. 예: `git commit -m "PM 세션(N차) 핸드오프 — …" -- .project_manager/wiki/log/current.md .project_manager/wiki/domain/<페이지>.md .project_manager/wiki/status.md`. 신설 파일은 `git add` 선행 필수이며 아니면 `pathspec … did not match`로 전체 commit이 rc=1 실패한다. `pm_state.md`는 gitignored. `(Co-Authored-By: Claude 트레일러)`.
- 마지막 응답에 인계 트리거를 코드블록으로 출력한다. 다음 세션은 `/pm-bootstrap`(claude·opencode) / `$pm-bootstrap`(codex) 실행; 인계 본문은 bootstrap이 log entry에서 dump한다.

## 동적 상태

진행 중인 의사결정과 남은 작업 전체 그림은 [`pm_state.md`](pm_state.md)에서 매 핸드오프 갱신한다. pm_role에는 정적 매뉴얼만 둔다.

## 다음 PM 세션 부트스트랩 프롬프트

[`pm_playbook.md`](pm_playbook.md) §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"을 `/pm-handoff`(claude·opencode) / `$pm-handoff`(codex)(backbone `pm_handoff.py`)가 자동 추출해 stdout 출력한다.

## 참고

- [`README.md`](README.md) — 디렉토리 의미
- [`architecture.md`](architecture.md) — 현재 아키텍처 단일 진실
- [`domain/`](domain/) — architecture 세부 concept·covers
- [`tickets/README.md`](tickets/README.md) — board 워크플로
- [`decisions/`](decisions/) — ADR
- `.claude/skills/pm-*/SKILL.md` — workflow command 정의
