# Changelog

이 프로젝트의 주요 변경 사항을 이 파일에 기록한다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 를 따르고,
버저닝은 [Semantic Versioning](https://semver.org/spec/v2.0.0.html) 을 따른다.

## [Unreleased]

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
