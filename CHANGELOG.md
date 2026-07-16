# Changelog

이 프로젝트의 주요 변경 사항을 이 파일에 기록한다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 를 따르고,
버저닝은 [Semantic Versioning](https://semver.org/spec/v2.0.0.html) 을 따른다.

## [Unreleased]

## [1.2.1] - 2026-07-16

v1.2.0 직후 backlog 소거 patch — 게이트 무결성·부트스트랩 오독 방지·nudge 능동화·문서 정합. 이중게이트(내부 reviewer + codex·codex 반려 2건 재작업 수렴) 통과·라이브 검증 포함.

### Fixed
- **external_review 빈-diff fail-loud** (T-0326) — 빈/공백 diff 를 외부 리뷰어 호출 전에 exit 1 로 차단(원인·조치 안내 포함). 분리 형상(adopter#0 등)에서 stale 사본 실행이 "변경 없음 통과"로 위장하던 false-green 원천 차단. dry-run 포함 무조건 fail.
- **pm_bootstrap `--branch`/`--resume` repo-가드 순서** (T-0327) — 가드를 auto-resolve 앞으로 이동, `--repo` 없는 호출이 자동바인딩 슬롯에 silent 부착되던 edge 차단(에러 문구 불변).
- **pm_bootstrap 보드 요약 open 라벨 오독 방지** (T-0331) — open 카운트 라벨을 `(공유 backlog·슬롯무관)` 으로 정정(보드 섹션+첫-turn 요약 양쪽·done/claimed 는 슬롯-스코프 유지) + **타 세션 진행(claimed) 현황 1줄** 병기(전용 무렌즈 조회·fail-soft) + pm-bootstrap 카드에 "board 숫자는 스냅샷 — 옵션 제시 전 `list --mine` 교차 확인" 지침. claimed 행 파서는 고정폭 컬럼 위치 기반(제목/tags 내용 불독·cmd_list 실행 통합 가드).
- **pm_import 치환-제외 목록 하드코딩 제거** (T-0329) — 방법론 문서 제외 집합을 치환 시점 dest 인스턴스 manifest 에서 파생(신규 방법론 문서 자동 편입·`--from` 흡수 경로 정합) + broken-manifest 폴백 floor. identity_args 로더 관용구 통일·pm_playbook 라벨 정렬 동반.

### Changed
- **graceful nudge 2단 강화** (T-0328·ADR-0037 확장) — hard-stop 직전 strong 밴드(`min(stop+3%p, nudge)` 파생·노브 추가 없음) 신설: "지금 즉시 `/pm-handoff`" 능동 유도·단계별 멱등·claude/opencode 파리티·statusline 빨강 "정지 임박" 표시.
- **nudge 주입-도달 라이브 durable 테스트** (T-0286) — on-demand(`PM_ORCH_LIVE=1`) probe 기반 시나리오 2건(claude 라이브 1회 실통과·opencode 는 upstream tool-loop hang 확증 후 skip 박제). CI 기본 skip·release pin 불변.
- **dual-harness guest 어댑터 갱신 채널 정식화** (T-0330·ADR-0058) — "엔진+host 어댑터=`pm_update` / dual-harness 로 얹은 guest 어댑터=`add-harness <harness>` 재실행(refresh·live-safe)" 를 출하 문서 5표면에 명시(guest-flavor 채택자는 기존대로 pm_update 전파·ADR-0054).

## [1.2.0] - 2026-07-16

슬롯 정체성 CLI 플래그를 **decomposed `--repo`/`--slot`** 단일 방식으로 통일한다([[ADR-0057]] supersede [[ADR-0043]]). 여러 세대에 걸쳐 누적된 `--session`(actor)·`--worktree-slot`·`--session-num` 별칭을 **BREAKING 제거**하고, 전 도구가 공용 `identity_args` 모듈로 수렴한다. T-0313 슬롯-모호 remedy 오안내가 근본 소멸. 이중게이트(내부 reviewer + codex) 통과.

### BREAKING
- **정체성 CLI 플래그 통일 → `--repo`/`--slot`** (ADR-0057) — 아래 구 별칭을 **제거**했다(back-compat 없음). 정체성 인자를 받는 전 도구(board·pm_bootstrap·pm_handoff·ticket_finish·pm_config·worktree_pool)가 공용 `identity_args` 로 수렴.

  | 구 (제거됨) | 신 (canonical) |
  |---|---|
  | `--session <repo>_<N>` (actor) | `--repo <repo> --slot <N>` |
  | `--worktree-slot work/<repo>_<N>` | `--slot <N>` (`--repo` 와 함께) |
  | `--session-num <N>` (pm_handoff 차수) | `--session-seq <N>` |
  | bare `--slot <N>` (`--repo` 없음) | `--repo <repo> --slot <N>` (단독 `--slot` 은 fail-loud) |

  - `--repo X --slot N` → 슬롯 정체성 `<repo>_<N>`. `--repo X` 단독(actor) → 그 repo 활성 슬롯이 정확히 1개면 자동 해소·≥2 또는 0 이면 fail-loud. `--slot` 단독(`--repo` 없음) → fail-loud. 인자 전무 → 기존 해소 체인(`$PM_SESSION_NAME` > 활성 슬롯 lease 1개 > `local.conf session=`)은 **불변**.
  - **free-form `--session <name>` CLI 제거** — 커스텀 세션명은 `$PM_SESSION_NAME` 환경변수(또는 `local.conf session=`)로 바인딩한다. `board.py claim` 은 이제 `--repo`/`--slot`(+ `--user`)만 받는다.
  - `--session-seq`(handoff 차수·뷰-무관)·하니스 `--session-id`(대화 연속성)는 정체성과 무관해 **유지**한다.
  - **채택자 마이그레이션**: `board.py claim --session myproj_1` → `board.py claim --repo myproj --slot 1` · `pm_handoff --session-num 19` → `--session-seq 19` · `--worktree-slot work/myproj_1` → `--repo myproj --slot 1`.
  - **다운스트림 lockstep** (finance/회사 등 채택자): BREAKING 이라 미갱신 어댑터/스크립트의 구 플래그 호출은 깨진다. `pm_update`(엔진 흡수) + `add-harness` refresh(어댑터 표면)로 흡수한 뒤 위 매핑대로 호출 표기를 갱신한다.

### Changed
- **공용 `identity_args` 모듈** (T-0322) — 정체성 인자 파싱(`add_identity_args`/discriminated `parse_identity`)과 리스 원장 읽기를 전 도구 단일 진실로 수렴(도구별 복붙 제거·DRY).
- **docs/skill/command-card + 어댑터·템플릿 전수 sweep** (T-0320) — 25개 shipped 표면(pm_role·pm_playbook·skills·CLAUDE.md·AGENTS.md·opencode command·tickets/README + templates)을 새 표기로 정합(drift-0·byte-identical). parity 가드(T-0319·28 테스트)가 도구-간 semantics 동형 + shipped old-flag 부재를 steady-state 로 잠근다(재발 시 red).

### Fixed
- **슬롯-모호 remedy 오안내 근본 소멸** (T-0313 → 통일 흡수) — handoff/ticket_finish·pm_config 의 fail-loud 가 실재하지 않는 플래그를 가리키던 오안내가, 통일된 `--repo`/`--slot` 로 근본 해소.

## [1.1.4] - 2026-07-15

채택자(v1.1.0) 버그 wave — prefix 대소문자 + 세션/슬롯 뷰 격리를 채택자 관점으로 정합. 다 실버그·v1.1.3 재현 확인. dual gate(내부 reviewer + codex) 통과.

### Fixed
- **prefix 대소문자 허용** (ADR-0055 amends ADR-0042) — 생성/rename 검증이 소문자-only 라, 보드에 대문자 prefix 티켓(`T-AAA-*`)이 존재·정상 list 되는데도 `board new --prefix AAA` 를 자기 도구가 거부하던 등록측(`_REPO_NAME_RE`)·파싱측(`_TICKET_PREFIX_RE`)과의 3중 문법 불일치를 정합. prefix 동일성 = **case-insensitive fold**(대문자 허용)·canonical(등록/최초-사용) case **보존**(저장 소문자 강제·ID 재번호 없음 → 기존 대문자 보드 무손실·마이그 0). 등록/rename/merge/delete/repo-add 는 case-only 근접중복 fail-loud. (T-0311)
- **세션/슬롯 뷰 user-first** (ADR-0056 refines ADR-0053) — 필터 뷰(`list --mine`/`--session`/`--slot`)의 querying identity 를 area_owner-derived 에서 **현재 사용자**(`user_name()`)로 고정하고, `--session`/`--slot` 을 **내 것 ∩ 그 슬롯**(claim: user AND slot)으로 좁힌다. **타 사용자는 어떤 필터 뷰에도 안 나온다**(전체는 무필터 `list` 전용). bootstrap `--slot N` 카운트가 슬롯 정체성으로 조회(라벨 "(slot N)")되고 커맨드 카드가 `--mine`(전 슬롯)/`--session`(∩ 이 슬롯) 을 구분한다. legacy 슬롯-only claim 은 진짜 solo(distinct ticket-user AND distinct area_owner 둘 다 ≤1)에서만 slot 매칭 포함(multi-user 는 strict-exclude·`migrate-identity` backfill). 채택자 실측 S1(bootstrap 카운트)·S2(claim 가시성)·S3(필터 축) 종결. (T-0312)

## [1.1.3] - 2026-07-14

multi-PM 다중사용자 격리 robustness 완결 — 값-연결(격리·전파·표기·livegate) 근본 재설계 + 라이브 게이트. 다중사용자 공유 board 에서 타 사용자 미claim 티켓이 세션 뷰에 유출되던 격리 깨짐을 근본 fix 하고, 어댑터 safety-훅이 조용히 낡던 전파 갭을 닫았다.

### Added
- **멀티유저 세션 뷰 격리** (ADR-0053) — 다중사용자 공유 board 에서 세션 뷰(`list --mine`/`--session`/`--slot`)가 타 user 미claim open 을 열람하지 않는다(소유 = area_owner ?? created_by · `_distinct_ticket_users`≥2 strict-exclude · solo 는 all-open degrade 보존). 단일 predicate `_ticket_is_mine`. (T-0302 core · T-0304 기계 격리 게이트[각 슬롯 실 생성→뷰 섞임 검증])
- **라이브 멀티유저 composite 게이트** — release-marked opencode 라이브 테스트가 2 user(alice/bob) 공유 board 에서 각자 티켓 생성 후 세션 뷰 섞임 격리를 실증(비-공허 가드). (T-0309)
- **어댑터 hook/driver 전파** (ADR-0032 Q3 · ADR-0054) — engine-mirror 훅/드라이버(ctx-guard·hard-stop·statusline·회귀 게이트·relay 드라이버)를 `@source` source-remap 채널로 framework-owned 전파 + manifest 자기전파(신 엔트리가 기존 채택자에 도달). 엔진 safety-훅 fix 가 채택자에 닿는 채널을 신설(frozen 근절). (T-0303 core · T-0305)
- **anti-degrade 진단 surface** — `board list` 가 다중사용자 strict-exclude/정체성 미해소 시 stderr loud-warn(remedy 포함·stdout 무오염) · `pm-config status` 가 정체성·isolation posture(registry 기준 · 실 격리는 `board list --mine` 이 authoritative). (T-0307)
- **fresh opencode 채택자 drift-0 e2e** — pm_import↔pm_update 렌더 drift-0(byte-identical) + hook/driver 채택자 도달을 machine e2e 로 박제. (T-0308)

### Fixed
- **opencode self-update @render 비대칭 근본fix** — opencode 없이 import 한 채택자(모델 미해소)의 self-update(`pm_update`)가 `@source` 재렌더에서 미해소 `{{OPENCODE_PRO_MODEL}}` 을 leak 으로 rc-fail 시켜 **엔진 update 까지 전멸**하던 회귀(위 hook/driver `@source` 전파가 유입)를 근본 fix. 줄-중화 로직을 단일 진실(`pm_render.neutralize_model_todo`)로 추출해 import↔self-update 대칭화 — 미해소 모델은 graceful TODO placeholder 로 넘겨(부분-graceful·엔진/타 어댑터 정상 update) self-update 가 성공한다. 렌더러 로드 실패는 fail-loud, `opencode_pro_model=` 빈값(오설정)은 leak 으로 표면화(false-green 근절). 릴리즈 라이브 게이트가 포착한 blocker. (T-0310)
- **livegate check↔record 단일소스** — `livegate check` 도 record 와 동일한 engine-root sidecar 해소를 공유해, 어느 board.py 사본/cwd 로 check 하든 push 보호훅이 기록한 파일을 읽는다. wrong-copy stale 오독(false-green/false-red)을 원천 차단. (T-0306)
- **settings.json auto-compact 토글 중복** — 정본 top-level `autoCompactEnabled` 로 단일화(env `DISABLE_AUTO_COMPACT` 중복 제거) + 출하 template critical env 존재를 검증하는 guard 테스트(권한-승인 재직렬화 드롭 fail-loud). (T-0300)
- **livegate cwd fail-loud + slot-key 표기 정합** — 다중슬롯에서 livegate cwd 해소 모호를 fail-loud, slot-key 표기 sweep. (T-0298 · T-0299)

## [1.1.2] - 2026-07-14

worktree add 타임아웃 false-kill 제거(3-layer) + worktree/lease 견고성(중단-안전·정합) + board submodule 자동 셋업.

### Added
- **`pm-import --new --board-submodule --board-remote <url>`** — `--new` 의 board(tickets+areas)를
  별도 git submodule(`.project_manager/board`)로 자동 셋업한다(두-git 분리·multi-PM 공유 board·ADR-0033).
  빈 remote 는 tickets 폴더구조+areas.md 를 seed(commit+push)하고, 기존 board remote 는 재사용(합류)하며,
  `submodule.<path>.ignore=all` 을 설정한다. 잘못된(비-board) remote 는 명확히 fail-loud. inline 기본은
  완전 무변경. (T-0297)
- **worktree add 타임아웃 튜닝 노브** — 엔진 `PM_GIT_TIMEOUT`(초·`none`=무제한)·claude 하네스
  `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS`·opencode 하네스
  `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` 를 노출한다(기본 30분·`/pm-env` 문서화). (T-0292·T-0293)

### Fixed
- **worktree add false-kill (타임아웃 3-layer)** — 대형 repo 의 `worktree add`(로컬 bare→full checkout·
  느린 디스크/VPN/Windows)가 *진행 중인데도* 짧은 고정 타임아웃(120초)에 죽던 것을 고쳤다. 엔진은
  console-visible 러너 + 관대한 튜닝 가능 타임아웃으로, 그걸 호출하는 하네스(claude·opencode) bash 툴
  타임아웃도 30분으로 상향해 3층 모두 정상-느린 op 을 false-kill 하지 않게 했다. (T-0292·T-0293)
- **부분·깨진 bare mirror 의 조용한 통과** — `repo add`/`worktree add` 가 bare mirror 를 *경로 존재*로만
  판정해, 중단된 clone(하네스 타임아웃·Ctrl-C)이 남긴 부분/빈 bare 를 조용히 재사용하고 나중 `worktree add`
  가 날 git 에러로 죽던 것을, 실 bare 검증(`rev-parse --is-bare-repository` + HEAD 해소)으로 fail-loud
  진단하게 고쳤다(파괴적 자동삭제 없음). (T-0294)
- **create_slot 중단 시 orphan + status 미탐지 + 번호 충돌** — worktree 생성이 외부 중단(Ctrl-C/kill)되면
  장부에 없는 orphan worktree 가 남고 `status` 가 이를 못 보며 다음 슬롯 생성이 번호 충돌하던 것을 —
  provisional lease(중단-안전) + git↔장부 reconcile(orphan/stale/incomplete surface·조회 전용) + 안전
  cleanup(`worktree prune-stale`)로 고쳤다. (T-0295)
- **worktree add → 슬롯 바인딩 안내 누락** — `worktree add` 성공 출력이 다음 필수 스텝
  (`/pm-bootstrap <repo> --slot <N>` 바인딩)으로 이어주지 않던 것을 안내 한 줄로 보강했다. (T-0296)

## [1.1.1] - 2026-07-13

라이브 도그푸딩·채택자 실사용에서 드러난 출하 결함 수정 (버그 wave).

### Fixed
- **opencode 서브에이전트 보고 절단** — opencode 전역 `tool_output` 기본 상한(2000줄/50KB)이
  서브에이전트(task) 반환도 절단해, researcher/reviewer 의 큰 보고가 오케스트레이터에 온전히
  도달하지 못하던 것을 상한 상향으로 해소했다. (T-0289)
- **출하 스킬/커맨드 Windows 표기 파리티** — PM 스킬(claude)·커맨드(opencode)에 Windows 런처
  `py`·`.cmd` 파사드 표기를 통일했다. Windows 세션이 literal `python3`/`.sh` 를 그대로 실행해
  가짜 shim 실패·재시도로 시간을 낭비하던 것을 막는다. (T-0288)
- **livegate record 기록 위치 seam** — 두-git(홈 + worktree) 토폴로지에서 `livegate record` 가
  push 보호훅의 read 위치와 다른 곳에 기록될 수 있어 거짓 성공을 찍고 push 순간에야 드러나던 것을,
  기록 위치를 훅 read 위치와 단일-소스로 정렬하고 불일치 시 fail-loud 로 거부하도록 고쳤다.
  단일-repo 채택자는 무변경. (T-0287)
- **릴리즈 절차 GitHub Release 단계 강제화** — 릴리즈 절차에서 GitHub Release 생성을 필수 단계로
  승격하고 완결 확인 단계를 추가했다. 태그만 push 되고 Release 객체가 누락되던 것을 막는다. (T-0290)
- **공유 채택 폴더의 다중 사용자 repo hydrate** — 하나의 채택 폴더를 여러 사람이 clone 해 쓸 때,
  레지스트리(`areas.md` · git-tracked · 공유)엔 repo 가 등록돼 있으나 bare mirror(`.repos/` · gitignore ·
  per-clone)가 없어 2번째 사용자가 repo 를 받지도 추가하지도 못하던 것을 고쳤다. `pm-config repo add
  <repo>` 가 `--git` 없이도 `areas.md` 에 기록된 URL 로 mirror 를 hydrate 하고(불일치 시 등록 URL 우선),
  `worktree add` 의 mirror-부재 에러가 그 해법을 안내한다. (T-0291)

## [1.1.0] - 2026-07-13

dual-harness(claude·opencode 병행) 채택 지원, worktree/submodule 풀 관리 도구,
스킬-우선 PM 운영 규율(ADR-0052), 그리고 라이브 도그푸딩에서 발견한 출하 버그 수정.

### Added
- **dual-harness 채택** (`pm_config add-harness <claude|opencode>`) — 이미 채택한 인스턴스에 두 번째
  하네스 어댑터를 나란히 추가한다. claude·opencode 를 한 인스턴스에서 병행 운영하고(엔진 공유·어댑터층만
  분기), imported 인스턴스는 소스를 upstream fallback + `--from` 으로 해소한다. (T-0269/0270/0271/0282)
- **worktree/submodule 풀 관리** — `pm-worktree` 스킬 + `worktree_pool.py`(`dev`/`sync` 서브커맨드):
  pool 의 submodule 을 selective 재동기하고, 작업 중 submodule 을 dev 브랜치로 지정해 재동기로부터
  보호하며, drift 난 detached submodule 을 pin 으로 수동 재동기한다. 부트스트랩이 슬롯 브랜치·upstream·
  submodule status 를 surface 한다. (ADR-0049/0050/0051·T-0275/0276/0277/0278)
- 릴리즈 라이브 게이트에 worktree/dual-harness 시나리오를 반영하고 케이스 수집 pin 을 cascade 했다. (T-0278)

### Changed
- **스킬-우선 PM 운영 규율** (ADR-0052) — PM 운영단계(claim/finish/qa/dev-delegate/handoff)는 스킬로
  invoke 하고 backbone CLI 직접 우회를 금지한다. `pm_role` 에 규율을 명문화하고, 부트스트랩 커맨드 카드가
  스킬-우선을 반영하며, durable 회귀 가드로 못박았다. (T-0279/0280/0281)
- 용어 정합 sweep — 잔여 표현을 표준으로 통일했다. (T-0268)

### Fixed
- **opencode ctx-guard 플러그인 로드** — 플러그인 export 를 함수로 교정(ESM shim + `lib/` core 분리)해
  실 opencode 세션에서 정상 로드되도록 했다(이전엔 유닛만 green·라이브 세션에선 로드된 적 없음). (T-0283)
- **부트스트랩 fresh-slot self-sufficiency** — 새 슬롯 부트스트랩 출력의 스크램블 placeholder 를 제거해
  첫 세션이 자족적으로 시작하도록 했다. (T-0284)
- **ticket_finish 두-git seam** — 다중슬롯에서 회귀 cwd 해소가 모호하던 것을 `--session`/`--no-pytest`
  로 해소했다(ADR-0027). (T-0285)
- **worktree/repo origin-freshness** 2건 — 슬롯·repo 의 upstream 신선도 판정 버그를 고쳤다. (T-0273/0274)

## [1.0.6] - 2026-07-10

세션 정체성 인자의 canonical 통일, 멀티-PM 차수·워크스페이스의 슬롯별 격리,
부트스트랩 커맨드 카드, 채택자 진입문서·어댑터 정합 수정.

### Added
- **`board.py reid <OLD-ID> <NEW-ID>`** — 오발행 티켓의 ID(번호·prefix 부여/변경/제거)를 무손실
  재부여한다. 파일명·frontmatter 는 물론 전 참조(`depends_on`/`blocks`·본문 wikilink·slug
  파일명·`wiki`/`log`)를 토큰 단위로 정확히 rewrite 한다. collision 시 중단, `--dry-run` 미리보기,
  board-git 백업, 홈 git clean 요구, 다른 세션이 claim 중이면 중단, 멱등.
- **부트스트랩 커맨드 카드** — 부트스트랩이 이 세션이 쓸 커맨드를 정체성 실값으로 채운 완성형
  카드로 dump 한다(남는 자리는 사용자가 넣을 `T-NNNN`·`<PFX>` 같은 값뿐). 숨은 전제(claim 은
  promote 선행 · prefix 조작은 홈 git clean · livegate record 는 케이스 수 pin · migrate-identity 는
  단일 세션) 경고와 '정체성이 필요 없는 커맨드' 목록, '상황→소스' 포인터를 담아 `--help` 없이
  바로 칠 수 있다.
- **`pm_handoff --normalize-session-anchors [--dry-run]`** — `pm_state` 의 차수 앵커 오형식(`N차차`)을
  `N차` 로 정규화하는 멱등·비파괴 유지보수 도구. 파서를 관대하게 만드는 대신 데이터를 원천에서
  정규화한다.
- **멀티-PM slot 대시보드** `wiki/log/dashboard.md` — 핸드오프가 자기 섹션(키=세션 정체성)만
  overwrite 하고(3~5줄 상한·다른 섹션은 byte 불변·append 아님), 부트스트랩이 '다른 활성 PM' 섹션을
  가볍게 dump 한다. 런타임 파생물이라 gitignore 되고 출하물에 포함되지 않는다. 솔로면 건너뛴다.
- 릴리즈 라이브 게이트(`PM_ORCH_LIVE_RELEASE=1`)에 **커맨드 카드 기반 사용성 시나리오** 2건 추가 —
  실 LLM 이 카드만 보고 첫 시도에 커맨드를 성공시키는지 두 하네스에서 확인한다(라이브 케이스 7→9).

### Changed
- **세션 정체성 인자를 canonical 하나로 통일** — 정체성을 받는 커맨드가 `--session <repo>_<N>`
  (정체성) · `--session-seq N`(차수) 표기로 일원화됐다. `pm_handoff` 에 `--session`·`--session-seq` 를
  신설했고, 솔로(미지정) 경로는 동작이 바뀌지 않는다. canonical 과 구형 alias 를 함께 주고 값이
  다르면 명확히 실패한다.
- **차수(PM N차)를 전역 카운터에서 슬롯별 시퀀스로 격리** — 핸드오프 로그 헤더에 정체성 태그
  `PM N차 (<repo>_<N>)` 가 붙고(솔로는 태그 생략 = 기존 헤더와 byte 호환), 부트스트랩이 자기 슬롯
  태그 entry 만 필터해 차수·인계 본문·`pm_state`·reattach 를 복원한다. 멀티-PM 두 슬롯이 같은
  N차 를 주장하던 문제가 사라진다. 식별 불가 시 기존 전역 동작을 보존한다.
- **멀티-PM 기본 규율을 '자기 공간 우선' 으로 확정** — 자기 티켓은 `board list --mine`, 상태는
  per-slot `pm_state`, 인계는 자기 슬롯 태그 handoff entry 로 운영하고, 다른 PM 과는 대시보드로만
  공유한다.
- **`pm_role.md` 축약** — 커맨드 표기를 부트스트랩 카드에 위임하고, 필독 문서를 `CLAUDE.md` +
  per-slot `pm_state` + `/pm-bootstrap` dump 셋으로 줄였다(`status`·`architecture` 는 '필요시 조회').
  '찾아가는 법' 절을 신설했다.
- opencode 라이브 모델 예시를 `ollama/glm-5.2:cloud` 로 교체 — `pm_import` 의 seed 주석과
  `--opencode-model` 도움말 예시 문자열.

### Deprecated
- `pm_handoff --worktree-slot` → **`--session`**. 구형 플래그는 무기한 alias 로 계속 수용된다(기존
  스크립트 무파손). canonical 과 값이 다르면 명확히 실패한다.
- `pm_handoff --session-num` → **`--session-seq`**. 무기한 alias 로 수용, 불일치 시 실패. 차수 인자를
  rename 한 것은 정체성 `--session` 과의 명명 충돌을 피하기 위함이다.

### Fixed
- `pm_bootstrap` 이 `/pm-bootstrap <repo> --slot N` 의 positional `<repo>` 를 수용한다 — 핸드오프가
  찍어주던 커맨드와 raw CLI 의 불일치를 수리했다(`--repo` 와 alias·둘 다 주면 값 일치 필수·
  무인자 자동바인딩은 그대로).
- 진입문서와 어댑터 카드·위임 스킬의 세션명 지시를 canonical `<repo>_<N>`(솔로는 `--session` 생략)로
  정합 — opencode 의 하드코딩 `--session pm`·산문형 `` `pm` 세션 `` 과 claude 진입문서의 자유형
  `--session session-B` 를 제거했다. 하드코딩 세션명은 repo 유도를 조용히 건너뛰게 만든다. 재유입은
  표기 형태(인자형·산문형·괄호형)에 무관하게 한 규칙으로 막는 테스트로 봉인했다.
- 진입문서와 티켓 안내의 세션 식별 우선순위 서술을 실제 코드 동작에 맞췄다 — 없어진 `<hostname>-<pid>`
  정체성 폴백을 빼고, 활성 슬롯이 하나면 그 세션으로 해소하는 단계와 슬롯이 여럿이면 저장값을 건너뛰어
  오귀속을 막는 규칙을 반영했다.
- `board.py list` 와 보드 렌더가 숫자 태그(`tags: [2026, cleanup]`)에 크래시하던 것을 고쳤다 — 태그를
  문자열로 안전하게 처리한다. `--tag` 필터도 숫자 태그를 매치한다.
- board CLI `--help` 위생 — ticket 인자 metavar 를 `T-NNNN` 으로 표기하고, `new --prefix` 도움말을
  작업 카테고리 재정의에 맞게 갱신했다. `list --session`(뷰 렌즈)이 쓰기 주체 `--session` 과 별개라는
  주의문도 다시 썼다. 핸들러 동작은 바뀌지 않았다.
- `pm-config init`/`update` 의 usage 줄이 내부 파일명(`board.py`·`pm_update.py`) 대신 실제 커맨드명
  (`pm-config`)으로 표기된다 — 에이전트가 칠 커맨드를 오인하지 않게.
- 채택자 진입문서·스킬 정합 — pm-regression 스킬이 존재하지 않는 커맨드(done→open 복구)를 안내하던
  것을 정직하게 표기하고, pm-wave-claim 의 필수 섹션을 6→3(목표·완료 조건·참고)으로 정정했다.
  ADOPT 하네스 기본값을 `claude` 로 명시하고, 하네스별 ctx 예산 키를 진입문서에 반영했다.
- opencode 어댑터 정합 — researcher 출하 파일 말미의 스트레이 태그 2줄 제거, README 의 서브에이전트
  개수 undercount 정정, 인스턴스 소유 루트 `.gitignore` 신설(claude 파리티). opencode 의 `--opencode-model`
  예시와 `spike-new` 커맨드의 설계 스파이크 생애주기(초안 편집 → 봉인 → 이후 불변) 서술을 claude 쪽과
  맞췄다 — opencode 채택자가 옛 모델 예시·옛 봉인 모델을 받던 것을 정정했다.

## [1.0.5] - 2026-07-07

하네스별 ctx 예산 분리, 티켓 prefix 의 작업-카테고리 재정의와 관리 도구, 채택자 제보 결함 수정.

### Added
- **`board.py prefix` 관리 도구** — `list`(현황: prefix 별 개수·번호 범위) ·
  `rename <A|none> <B|none>`(카테고리 개명·이름 씌우기/지우기) · `strip <A>`(=rename A none) ·
  `merge <A> [B...] --into <T|none> [--reorder-chronological]`(created 순 통합·기본 append) ·
  `delete <A>`(빈 prefix 등록 제거). 전 동사 `--dry-run` 규모 미리보기, 참조 rewrite 는
  전 표기형(frontmatter·wikilink·본문·파일명) 토큰 단위 정확 치환, collision 시 중단,
  board-git 백업 커밋, 티켓 물리 삭제 없음(무손실 relabel). 혼재 보드(legacy `T-NNNN` +
  prefixed)를 시간순으로 합칠 수 있다.
- ctx 예산 **하네스별 오버라이드 키** — `ctx_window_tokens_claude` / `ctx_window_tokens_opencode`.
  한 repo 를 claude·opencode 로 동시 운용할 때 하네스별로 다른 예산을 준다. 미설정 시
  generic `ctx_window_tokens` → 200000 순으로 해소된다.
- `pm_log.py archive --keep-last N` — 날짜 대신 개수 기준으로 최근 N entry 만 남기고 봉인.
- prefix 사용 가이드(`pm_role.md`) — 언제 prefix(배타 카테고리)/tag(겹침 속성)/none(기본)을
  쓰는지, 남발 방지 수칙, 어댑터 마이그레이션 절차.

### Changed
- **티켓 prefix 의 의미를 재정의** — repo 네임스페이스 전용에서 **작업 카테고리**(M 무관·
  자유 입력·티켓당 1개)로. `repo add` 의 repo 명 prefix 자동 시드를 폐지하고, 명시
  `--prefix` 의 "등록값 강제"를 제거했다(형식 sanity `^[a-z0-9][a-z0-9_]*$` 와 예약어
  `none` 거부만 유지).
- **ctx 정지/넛지의 분모를 해소된 예산 하나로 통일** — claude statusLine(물리 window %
  표시 폐기)·claude hook·opencode plugin(`modelLimit()` 물리한도 조회 폐기)이 전부 같은
  예산을 쓴다. 표시와 정지가 같은 숫자로 움직인다. 큰 window 는 예산 키를 명시한다.
- domain 스캔이 frontmatter 없는 `.md`(tmp·메모)를 개별 경고 없이 조용히 건너뛰고
  디렉토리별 개수 요약 1줄만 남긴다(malformed 는 개별 경고 유지).

### Fixed
- 채택자 제보 결함 — `external_review.py`/`ticket_finish.py` 의 repo 루트 하드코딩
  (`.project_manager` 마커 상향 탐색으로 교체·venv 부재 폴백 명문화) · `pm_handoff` step3
  앵커 정확-일치 실패 시 핸드오프 전체가 죽던 것(정규화 부분일치 + fail-soft 로 완주) ·
  prefixed 티켓 ID lint 정합(회귀-lock).
- non-UTF-8 파일이 domain 스캔·참조 rewrite 를 크래시시키던 경로(graceful skip + 경고).
- relabel(대량 ID 변경)의 동시성 — 스캔·충돌 검사·적용 전체를 board 락 안 fresh snapshot
  으로 직렬화하고, 적용 직전 대상 경로 점유를 재검증해 덮어쓰기를 원천 차단.

## [1.0.4] - 2026-07-03

세션 정체성 유도 전환과 사람-친화 문서 개편.

### Added
- CHANGELOG(이 파일)와 GitHub Releases — 릴리즈 절차에 노트 단계가 포함된다(v1.0.0~1.0.3 소급).
- MIT 라이선스.
- README 전면 개편 — 문제(compaction)와 해결을 앞세운 사람-친화 구성, 절별 프롬프트 예시,
  Mermaid 다이어그램. 기계 절차 reference 는 `docs/` 4파일로 분리.

### Changed
- **세션 정체성·티켓 prefix 를 저장값에서 유도값으로 전환** — `local.conf` 의
  `session=`/`prefix=` 는 solo 전용 legacy 로 강등. 활성 슬롯이 정확히 1개면 그 세션으로
  자동 유도되고, 여러 개면(멀티 홈) 명시 없는 귀속 조작이 명확히 실패한다(silent 오귀속
  차단). 멀티 홈은 두 키를 제거해도(남아도 무시) 동작이 같다.
- 멀티 홈의 push 회귀 게이트가 **전 활성 슬롯 all-or-nothing** 으로 동작한다 — 기록 확인
  우선(저비용)·미검증 슬롯만 실행·하나라도 red 면 차단. 게이트 좁히기는 CLI `--session`
  명시로만 가능하고 환경변수로는 좁혀지지 않는다.
- 티켓 prefix 는 areas.md 의 repo 등록이 단일 진실 — 등록 repo 가 1개면 자동 적용되고,
  여러 개면 세션에서 유도하며, 모호하면 발행이 명확히 실패한다.
- Windows PowerShell 안내 정합 — `.\pm-config.cmd`/`.\pm-update.cmd` 진입을 문서에 표기하고,
  PowerShell 5.x 의 `&&` 체이닝 미지원(ParseError) 주의를 진입 문서에 추가.

### Fixed
- 멀티 슬롯 홈에서 비바인딩 세션이 `local.conf` 의 세션명을 물려받아 남의 세션으로
  자기 식별하던 문제.
- 미바인딩 상태의 `repo add` 가 등록 owner 를 문자열 "None" 으로 기록할 수 있던 경로 —
  부작용 전에 명확히 중단한다.

## [1.0.3] - 2026-07-03

게이트 하드닝 + 릴리즈 단일 라이브 게이트(기계 강제).

### Added
- `board.py livegate record` / `check` — 릴리즈 라이브 테스트 wave 를 실측하면 특정 엔진
  rev 에 pin 된 기계 검증 green 마커가 기록되고, 보호 브랜치 pre-push 훅이 릴리즈 push 전에
  이를 소비한다.
- 부트스트랩이 git freshness 를 surface — fetch 해서 upstream 대비 얼마나 뒤처졌는지 보고하고
  안전한 fast-forward 동기를 안내한다. fresh-clone 연속성 포함.

### Changed
- 라이브 테스트를 릴리즈 단일 게이트로 통합 — 별도 "shipping" tier 는 폐지. 보호 브랜치 push 는
  push 대상 rev 에 기록된 라이브 green 을 요구한다(라이브-무관/핫픽스 변경만 문서화된 우회 허용).
- 핸드오프가 더 이상 차단형 shipping 테스트를 돌리지 않는다 — 비차단 1줄 안내로 대체되어
  핸드오프가 다시 빨라졌다.
- 렌더된 어댑터 파일이 머신-불변이 됐다(인터프리터·테스트 명령 placeholder 중립화) —
  clone 간 재렌더가 파일을 더 이상 뒤흔들지 않는다.

### Fixed
- `pm_render` 가 누락된 `local.conf` 키를 빈 문자열로 조용히 치환하지 않는다 — 명확히
  실패하거나 경고한다.
- 수집된 테스트가 0개일 때 회귀 실행이 false pass 를 기록하지 않는다.

## [1.0.2] - 2026-07-02

Windows 전체 지원.

### Added
- Windows 지원 — 프레임워크가 Windows(네이티브·Git Bash)에서 동작한다: 인터프리터 해소가
  `py` 런처를 우선하고, UTF-8/cp949 인코딩을 엔진 코드가 처리하며, 셸 facade 와 git 훅이
  정확히 실행된다.

### Changed
- ticket claim 이 Windows 에서도 직렬화된다 — 동시 세션 간 claim 배타성 보존.
- context-guard 임계를 상향해 hard stop 전 여유를 늘렸다.

### Fixed
- board git-sync 가 detached HEAD 를 오프라인으로 오진하지 않고, orphan 커밋을 조용히
  누적하지 않는다.
- hard-stop 이 핸드오프 자체를 잠그지 않는다.
- Windows 경로 처리 정규화(render·update 의 백슬래시 경로).

## [1.0.1] - 2026-07-01

### Added
- graceful handoff nudge — 컨텍스트 예산 nudge 임계에서 비차단 안내가 현재 단계 마무리와
  핸드오프를 유도한다(hard stop 전·Claude Code + opencode).

### Changed
- hard-stop 이 새 작업만 정지한다 — 핸드오프 도구는 예외 통과라 세션이 항상 깨끗하게
  인계할 수 있다.
- 엔진 freshness 는 git rev-baseline 단일 추적 — 중복 버전-파일 마커와
  `pm_update --version` 플래그 제거.

### Fixed
- `board init` / `pm-config init` 재실행이 `local.conf` 를 덮어쓰지 않는다 — 사용자·운영
  설정을 비파괴 병합한다.
- multi-PM 핸드오프 프롬프트에 대상 슬롯이 포함된다 — 다음 세션이 모호함 없이 worktree 를
  해소한다.

## [1.0.0] - 2026-06-28

첫 안정 릴리즈.

### Added
- PM 오케스트레이션 프레임워크: ticket 보드 + wiki 지식 베이스 + ADR(Architecture Decision
  Records) + 살아있는 domain 지식 레이어.
- 멀티-하니스 지원 — Claude Code·opencode 어댑터가 하나의 엔진을 공유.
- 멀티-PM 운용: 여러 저장소 × 여러 PM 세션, worktree 풀 + 슬롯 lease 기반.
- self-sufficient 부트스트랩·핸드오프 — 새 세션이 자기 컨텍스트(슬롯·차수·직전 인계)를
  자동 해소해 컨텍스트 한계를 넘어 연속성이 유지된다.
- 이중 게이트 코드리뷰 + 3-tier 테스트 워크플로(단위 회귀·smoke·라이브 릴리즈 wave).
