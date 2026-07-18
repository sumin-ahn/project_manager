---
name: pm-release
description: "릴리즈 절차 명령어化 — adopter#0 sync 선행 → livegate record(readonly 슬롯 핀·수집 N 확인) → main push(대화 승인·자동 안 함) → tag → gh release create → gh release view 완결 확인 → adopter#0 흡수 → audience 라벨. backbone = board.py livegate record/check + pm_update(=/pm-update). 공개 main push 는 사용자 승인 게이트 유지. Triggers: '릴리즈 내', 'release vX.Y.Z', '태그·GitHub Release', 'pm-release'."
audience: pm-internal
---

# /pm-release — 릴리즈 절차 (순서 고정·명령어化)

> 릴리즈 수동 체인(adopter#0 sync 선행 → livegate record → main push → tag → GitHub Release
> 생성/완결 확인 → adopter#0 흡수)을 **순서 고정** 절차로 codify 한다. 비즈니스 로직 0 — 기존
> 엔진 CLI(`board.py livegate record/check` · `pm_update`=[[pm-update]]) + `git`/`gh` 호출을
> 얇게 감싼다 (ADR-0049 명령어化 4요소). **공개 main push 는 사용자 승인 게이트를 유지** — 이 스킬은
> 절차를 자동화하되 **push 를 대신 실행하지 않는다**(보호훅 + livegate 기계 이중 안전 불변).

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> `./pm-update.sh` 파사드는 bash 용 — PowerShell/cmd 에선 **`.\pm-update.cmd`**(동일 인자).
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 릴리즈를 낼 때 invoke. 사용자가 "vX.Y.Z 릴리즈 내자"·"태그
찍고 GitHub Release 만들어" 라고 지시하면 PM 이 이 스킬을 부른다. 셋업(user-entrypoint)의 확장이
아니라 **운영중-관리** 스킬이다(ADR-0049).

## 사용 시점 (trigger)

- 보호 브랜치(`main`)로 릴리즈를 낼 때 — CHANGELOG 절 확정 후 push→tag→GitHub Release 를 낼 준비가 됐을 때.
- v1.3.0 자신이 이 스킬의 첫 도그푸딩 소비자다(PM 71 의 3연속 patch 수동 릴리즈 고통이 근거).

## 실행 절차 (순서 고정 · ADR-0039)

> 아래 순서를 지킨다. 각 단계는 기존 backbone 을 호출하고, **push 순간은 사용자 승인을 기다린다**.

### 1. adopter#0 sync 선행 ([[pm-update]] · false-green/false-block 차단)

codex/livegate 는 PM 홈 import 사본이 stale 이면 **false 결과**(빈 diff false-green·stale-pin
false-block)를 낸다 — livegate record 를 돌리기 **전에** ② PM 홈을 worktree canonical 에 동기한다.

```bash
./pm-update.sh --from <worktree-canonical-경로>     # 예 work/project_manager_1 (upstream=경로면 --from 생략 가능)
```

- 이 단계는 **기계 단계로 박아** stale-사본 false-green/false-block 을 원천 차단한다 — 건너뛰지 않는다.
- 상세(freshness 분기·manifest reconcile·drift)는 [[pm-update]] 스킬 절차를 따른다.

### 2. livegate record (readonly 슬롯 핀 · 수집 N 확인 · false-green 방지)

릴리즈 라이브 wave 를 **실측**해 green(수집 pin 충족)을 push 대상 rev 에 기록한다(손기록 없음·보호훅이 소비).

**main-참조 역할은 readonly 슬롯이 진다(T-0358·T-0360)**: 부트스트랩 0단계가 main 직접 checkout·
origin-추적 슬롯 진입을 거부하므로, 릴리즈 라이브 tier·codex `--paths` 가 돌던 "origin/main 이 도는
슬롯" 역할은 readonly 공유 슬롯(detached·origin/main 기준면)으로 이전됐다. readonly 슬롯은 무리스
(unleased)라 `--repo --slot` 로 해소되지 않으므로 **`--cwd <readonly 슬롯 절대경로>`** 로 핀한다.

```bash
# main-참조 기준면 = readonly 슬롯(T-0358·detached·origin/main 추적). PM_ORCH_LIVE_RELEASE=1 필수.
PM_ORCH_LIVE_RELEASE=1 python3 .project_manager/tools/board.py livegate record --repo <repo> --cwd <readonly 슬롯 절대경로>
#   readonly 슬롯 경로는 `pm-config worktree status` 의 role=readonly 행에서 확인.
```

- **readonly 슬롯 핀(`--cwd`)**: 무리스 공유 슬롯이라 `--slot` 로 안 잡힌다 — `--cwd <절대경로>` 로 명시한다.
  무명시 + leased ≥2 이면 seam 이 fail-loud(모호는 시끄럽게)한다. 침묵 폴백 금지.
- **codex `--paths` 도 readonly 슬롯 기준**: 릴리즈 전 codex 교차검증(`external_review`)의 `--paths` 도
  같은 readonly 슬롯 worktree 를 가리킨다 — canonical(origin/main) 읽기 기준면이 그리로 이전됐다(stale
  import 사본 false 결과 방지·§1 sync 선행과 짝).
- **`PM_ORCH_LIVE_RELEASE=1`**: 이게 없으면 release wave 가 skip 되어 **수집 N=0** → record 가
  fail(수집 위장 차단)한다. 라이브 tier(claude+opencode)를 실제로 태우려면 반드시 set.
- **수집 N 확인(false-green 방지)**: board 가 `release N/<pin> green ✓` 를 출력한다 — N 이 pin 과
  다르면(마커 소실·wrong-cwd) fail 로 릴리즈를 막는다. 출력의 N==pin 을 눈으로 확인해 보고한다.
- 라이브 tier = **release 단일**·dual-harness(claude+opencode) 실측(회사 기준 버전·격리 `--dir`·glm-5.2).

### 3. main push (대화 승인 · 자동 안 함) → tag → GitHub Release

먼저 CHANGELOG 절 확정(`[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`·채택자-관점 요약·티켓/내부 세션 용어
유입 금지 + 새 빈 `[Unreleased]`). 그다음:

1. **main push — 사용자 승인 게이트(이 스킬이 자동 실행하지 않는다)**. 보호훅이 `livegate check`
   green(§2 기록)을 추가로 요구한다. push 커맨드는 **사용자 승인 후** PM 이 실행한다:
   ```bash
   # 사용자 승인 필요 — 스킬은 이 명령을 대신 돌리지 않는다(보호훅 + livegate 이중 안전).
   PM_ALLOW_PROTECTED_PUSH=1 git push origin main
   ```
2. **annotated tag** `vX.Y.Z` push (`git tag -a vX.Y.Z -m ... && git push origin vX.Y.Z`).
3. **GitHub Release 생성**(필수·태그만으론 릴리즈 아님·T-0290):
   ```bash
   gh release create vX.Y.Z --notes-file <CHANGELOG 해당 절 추출> --verify-tag
   ```
   gh 미인증이면 생략하지 말고 사용자에게 넘기되 "릴리즈 미완료"로 명시한다.
4. **릴리즈 완결 확인**(T-0290 — "만들었다" 주장 감사):
   ```bash
   gh release view vX.Y.Z      # Release 객체가 실제로 존재해야 릴리즈 종료
   ```
   livegate·push·tag 는 기계 게이트가 강제하지만 GitHub Release 는 원격 상태 행위라 강제되지 않는다 —
   이 확인으로 릴리즈를 닫는다.

### 4. adopter#0 흡수 ([[pm-update]])

릴리즈 커밋이 worktree canonical 에 안착한 뒤, ② PM 홈에 다시 흡수한다:

```bash
./pm-update.sh --from <worktree-canonical-경로>     # 릴리즈된 엔진/방법론을 ②로 반영
```

### 5. audience 라벨 (ADR-0049)

명령어化로 신설/변경한 스킬·커맨드의 frontmatter `audience: user-entrypoint | pm-internal` 를
확정한다(청중=binary·명령어化로 새로 느는 건 대개 pm-internal).

## 결정

- **공개 main push = 대화 승인 유지(불변)**: 스킬이 절차를 자동화하되 push 순간은 사용자 승인 게이트
  (보호훅 T-0076 + livegate 기계)의 **이중 안전을 깨지 않는다** — 스킬이 자동 push 하지 않는다
  (② private 만 자율·① public main 은 승인). `PM_ALLOW_PROTECTED_PUSH=1`·`PM_SKIP_LIVE_GATE=1` 은
  PM 이 스스로 쓰지 않는다(사용자 명시 OK 의 escape hatch·환경 문제는 우회 사유 아님).
- **adopter#0 sync 선행을 기계 단계로 박아** stale-사본 false-green/false-block 을 원천 차단(§1·§4).
- **라이브 tier = release 단일**: livegate record 가 `pytest -m release`(dual-harness)를 실측·기록하고,
  보호훅이 push HEAD 의 green 을 `livegate check` 로 재확인한다(record=기록·check=소비).

## 잔여 PM 손

- 스킬은 backbone 호출 + 승인 대기 절차 — 각 단계 stdout(livegate 수집 N·gh release view 결과)을
  PM 이 읽고 사용자에게 보고한다.
- **main push 실행·CHANGELOG 절 문안·GitHub Release 노트**는 PM/사용자가 확정·승인한다(스킬 자동 아님).

## 참고

- backbone: `board.py livegate record`/`livegate check`(ADR-0039) · `pm_update`([[pm-update]]) · `git`/`gh`.
- ADR-0049(명령어化 4요소·청중) · ADR-0039(livegate·라이브 tier 단일) · T-0290(gh release view 완결) ·
  T-0344(opencode command pair-pin) · T-0360(main-참조 역할 readonly 이전·거부 활성). 라이브 하네스
  테스트 = `tests/test_pm_release_live.py`(ADR-0050).
- 보호훅 = 보호 브랜치 push 차단 + `livegate check` green 요구(pm_role §릴리즈 절차).
