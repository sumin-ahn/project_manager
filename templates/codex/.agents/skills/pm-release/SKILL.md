---
name: pm-release
description: "릴리즈 절차 명령어化 — adopter#0 sync 선행 → livegate record(readonly 슬롯 핀·수집 N 확인) → main push(대화 승인·자동 안 함) → tag → gh release create → gh release view 완결 확인 → adopter#0 흡수 → audience 라벨. backbone = board.py livegate record/check + pm_update(= pm-update 스킬). 공개 main push 는 사용자 승인 게이트 유지. Triggers: '릴리즈 내', 'release vX.Y.Z', '태그·GitHub Release', 'pm-release'."
audience: pm-internal
---

# $pm-release — 릴리즈 절차 (순서 고정·명령어化)

기존 엔진 CLI(`board.py livegate record/check` · `pm_update`=[[pm-update]])와 `git`/`gh`를 순서대로 호출한다. **공개 main push는 사용자 승인 게이트를 유지하며 이 스킬이 대신 실행하지 않는다.** 청중은 릴리즈를 내는 PM(`pm-internal`)이다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> `./pm-update.sh`·`./pm-config.sh` 파사드는 bash 용 — PowerShell/cmd 에선 각각
> **`.\pm-update.cmd`**·**`.\pm-config.cmd`**(동일 인자).
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 실행 절차 (순서 고정)

### 1. adopter#0 sync 선행 ([[pm-update]])

livegate record 전에 PM 홈을 worktree canonical에 동기해 stale import 사본의 빈 diff false-green·stale-pin false-block을 막는다. 건너뛰지 않으며 freshness·manifest reconcile·drift 분기는 [[pm-update]]를 따른다.

```bash
./pm-update.sh --from <worktree-canonical-경로>     # 예 work/project_manager_1 (upstream=경로면 --from 생략 가능)
./pm-config.sh sync-adapter-config --check --from <worktree-canonical-경로>
```

두 번째 명령 rc=0이 adopter#0 흡수 완료 조건이다. red면 livegate/main push/tag/GitHub Release로
진행하지 않고 `--accept` 또는 pm-update 재실행 처방으로 먼저 수렴시킨다.

### 2. livegate record (readonly 슬롯 핀·수집 N 확인)

push 대상 rev의 release live wave를 실측·기록한다. 부트스트랩 0단계가 main 직접 checkout·origin-추적 슬롯 진입을 거부하므로 main 참조 기준면은 readonly 공유 슬롯(detached·origin/main)이다. 이 슬롯은 무리스(unleased)라 `--repo --slot`로 해소되지 않으므로 `--cwd <readonly 슬롯 절대경로>`로 핀한다.

```bash
# main-참조 기준면 = readonly 슬롯(detached·origin/main 추적). PM_ORCH_LIVE_RELEASE=1 필수.
PM_ORCH_LIVE_RELEASE=1 python3 .project_manager/tools/board.py livegate record --repo <repo> --cwd <readonly 슬롯 절대경로>
#   readonly 슬롯 경로는 `pm-config worktree status` 의 role=readonly 행에서 확인.
```

- readonly 슬롯은 `--cwd <절대경로>`로 명시한다. 무명시 + leased ≥2이면 seam이 fail-loud한다. 침묵 폴백 금지.
- 릴리즈 전 추가 리뷰어 교차검증(`external_review`)의 `--paths`도 같은 readonly 슬롯 worktree를 가리킨다.
- `PM_ORCH_LIVE_RELEASE=1`이 없으면 release wave가 skip되어 수집 N=0, record가 fail한다.
- board 출력 `release N/<pin> green ✓`의 N==pin을 눈으로 확인해 보고한다. 다르면 마커 소실·wrong-cwd이므로 fail로 릴리즈를 막는다.
- live tier는 **release 단일**이고 **3 하네스 실측**이다 — claude·opencode 는 wave 전 구간(부트스트랩~핸드오프·multi-repo·multi-user), codex 는 위임 완주와 relay 마커 정체성 2건으로 커버 깊이가 얕다. opencode 는 회사 기준 버전·격리 `--dir`·glm-5.2 로 돈다.

### 3. CHANGELOG → main push → tag → GitHub Release

먼저 CHANGELOG를 `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`로 확정하고 채택자 관점으로 요약한다. 티켓/내부 세션 용어를 넣지 말고 새 빈 `[Unreleased]`를 만든다.

1. **main push — 사용자 승인 게이트(이 스킬이 자동 실행하지 않는다).** 보호훅은 `livegate check` green을 추가 요구한다. 승인 후 PM이 실행한다.

   ```bash
   # 사용자 승인 필요 — 스킬은 이 명령을 대신 돌리지 않는다(보호훅 + livegate 이중 안전).
   PM_ALLOW_PROTECTED_PUSH=1 git push origin HEAD:main
   ```

   refspec을 `HEAD:main`으로 명시한다. 릴리즈/task 브랜치에 체크아웃한 상태에서 `git push origin main`은
   지금 커밋이 아니라 **낡은 로컬 `main`을 민다** — 첫 main push가 그 이유로 거부된 실사건이 있다.

2. **annotated tag** `vX.Y.Z`를 push하며 refspec을 명시한다:
   `git tag -a vX.Y.Z -m ... && git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z`
   릴리즈 브랜치명=태그명일 때 `git push origin vX.Y.Z`는 `"src refspec matches more than one"`으로 실패한다.

3. **GitHub Release 생성**은 필수다(태그만으로 완료 아님).

   ```bash
   gh release create vX.Y.Z --notes-file <CHANGELOG 해당 절 추출> --verify-tag
   ```

   gh 미인증이면 생략하지 말고 사용자에게 넘기며 **릴리즈 미완료**로 명시한다.

4. **릴리즈 완결 확인**:

   ```bash
   gh release view vX.Y.Z      # Release 객체가 실제로 존재해야 릴리즈 종료
   ```

   GitHub Release는 원격 상태 행위라 livegate·push·tag처럼 기계 강제되지 않으므로 이 확인 뒤 닫는다.

### 4. adopter#0 흡수 ([[pm-update]])

릴리즈 커밋이 worktree canonical에 안착한 뒤 PM 홈에 다시 흡수한다.

```bash
./pm-update.sh --from <worktree-canonical-경로>     # 릴리즈된 엔진/방법론을 반영
./pm-config.sh sync-adapter-config --check --from <worktree-canonical-경로>
```

마지막 `--check` rc=0까지가 adopter#0 흡수 완료다. red 상태를 릴리즈 완료로 기록하지 않는다.

### 5. audience 라벨

신설/변경한 스킬·커맨드 frontmatter의 `audience: user-entrypoint | pm-internal`를 확정한다(청중=binary, 명령어化로 늘어나는 것은 대개 `pm-internal`).

## 불변·보고

- 공개 main push는 대화 승인 + 보호훅/livegate 이중 안전을 유지한다(private만 자율). `PM_ALLOW_PROTECTED_PUSH=1`·`PM_SKIP_LIVE_GATE=1`은 PM이 스스로 쓰지 않는다(사용자 명시 OK의 escape hatch이며 환경 문제는 우회 사유가 아니다).
- livegate record는 `pytest -m release`(claude·opencode·codex 3 하네스)를 실측·기록하고 보호훅은 push HEAD의 green을 `livegate check`로 재확인한다(record=기록·check=소비).
- PM은 각 단계 stdout, 특히 livegate 수집 N과 `gh release view` 결과를 읽어 보고한다.
- main push 실행·CHANGELOG 문안·GitHub Release 노트는 PM/사용자가 확정·승인한다.
- backbone: `board.py livegate record`/`livegate check` · `pm_update`([[pm-update]]) · `git`/`gh`; 라이브 하네스 테스트는 `tests/test_pm_release_live.py`; 보호훅은 보호 브랜치 push 차단 + `livegate check` green을 요구한다(pm_role §릴리즈 절차).
