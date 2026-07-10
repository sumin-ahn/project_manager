# Changelog

이 프로젝트의 주요 변경 사항을 이 파일에 기록한다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 를 따르고,
버저닝은 [Semantic Versioning](https://semver.org/spec/v2.0.0.html) 을 따른다.

## [Unreleased]

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
- 진입문서와 어댑터 카드의 세션명 지시를 canonical `<repo>_<N>`(솔로는 `--session` 생략)로 정합 —
  opencode 의 하드코딩 `--session pm` 과 claude 진입문서의 자유형 `--session session-B` 를 제거했다.
  하드코딩 세션명은 repo 유도를 조용히 건너뛰게 만든다. 재유입은 테스트로 막는다.
- board CLI `--help` 위생 — ticket 인자 metavar 를 `T-NNNN` 으로 표기하고, `new --prefix` 도움말을
  작업 카테고리 재정의에 맞게 갱신했다. `list --session`(뷰 렌즈)이 쓰기 주체 `--session` 과 별개라는
  주의문도 다시 썼다. 핸들러 동작은 바뀌지 않았다.
- `pm-config init`/`update` 의 usage 줄이 내부 파일명(`board.py`·`pm_update.py`) 대신 실제 커맨드명
  (`pm-config`)으로 표기된다 — 에이전트가 칠 커맨드를 오인하지 않게.
- 채택자 진입문서·스킬 정합 — pm-regression 스킬이 존재하지 않는 커맨드(done→open 복구)를 안내하던
  것을 정직하게 표기하고, pm-wave-claim 의 필수 섹션을 6→3(목표·완료 조건·참고)으로 정정했다.
  ADOPT 하네스 기본값을 `claude` 로 명시하고, 하네스별 ctx 예산 키를 진입문서에 반영했다.
- opencode 어댑터 정합 — researcher 출하 파일 말미의 스트레이 태그 2줄 제거, README 의 서브에이전트
  개수 undercount 정정, 인스턴스 소유 루트 `.gitignore` 신설(claude 파리티).

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
