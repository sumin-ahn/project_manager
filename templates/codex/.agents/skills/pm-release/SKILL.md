---
name: pm-release
description: "릴리즈 절차 명령어化 — adopter#0 sync 선행 → livegate record(readonly 슬롯 핀·수집 N 확인) → main push(대화 승인·자동 안 함) → tag → gh release create → 관리자 자산 3종 도착 확인(release-manager.yml) → gh release view 완결 확인 → adopter#0 흡수 → audience 라벨. backbone = board.py livegate record/check + pm_update(= pm-update 스킬). 공개 main push 는 사용자 승인 게이트 유지. Triggers: '릴리즈 내', 'release vX.Y.Z', '태그·GitHub Release', 'pm-release'."
audience: pm-internal
---

# $pm-release — 릴리즈 절차 (순서 고정·명령어化)

기존 엔진 CLI(`board.py livegate record/check` · `pm_update`=[[pm-update]])와 `git`/`gh`를 순서대로 호출한다. **공개 main push는 사용자 승인 게이트를 유지하며 이 스킬이 대신 실행하지 않는다.** 청중은 릴리즈를 내는 PM(`pm-internal`)이다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행 절차 (순서 고정)

### 1. adopter#0 sync 선행 ([[pm-update]])

livegate record 전에 PM 홈을 worktree canonical에 동기해 stale import 사본의 빈 diff false-green·stale-pin false-block을 막는다. 건너뛰지 않으며 freshness·manifest reconcile·drift 분기는 [[pm-update]]를 따른다.

```bash
./pm-update.sh --from <worktree-canonical-경로>     # 예 work/<repo>_<N> (upstream=경로면 --from 생략 가능)
./pm-config.sh sync-adapter-config --check --from <worktree-canonical-경로>
```

두 번째 명령 rc=0이 adopter#0 흡수 완료 조건이다. red면 livegate/main push/tag/GitHub Release로
진행하지 않고 `--accept` 또는 pm-update 재실행 처방으로 먼저 수렴시킨다.

### 2. livegate record (플랫폼·라이브 검증)

push 대상 rev의 선언 플랫폼 전체와 release live wave를 실측·기록한다. qa.platforms가 선언됐으면 같은 rev의 core green을 먼저 확인하고 플랫폼을 선언 순서대로 실제 실행한다. 플랫폼 red/미실행·수집·HEAD·명령·좌표 불일치는 라이브 테스트 전에 거부한다. 이전 green을 새 실패가 가리지 않게 무효화하며 livegate check도 현재 선언과 같은 rev의 core·플랫폼 결과를 재검증한다.

Windows 전체 검증은 모든 구현·통합이 끝난 릴리즈 직전에 실행한다. 개발 회귀는 현재 플랫폼의 test_cmd만 백그라운드로 실행하고 다른 작업 트리의 개발을 계속한다. 검증 트리를 동결하고 완료·통합 시 결과를 확인한다. 새 작업 큐나 플랫폼 선택 옵션을 추가하지 않는다. 부트스트랩 0단계가 main 직접 checkout·origin-추적 슬롯 진입을 거부하므로 main 참조 기준면은 readonly 공유 슬롯(detached·origin/main)이다. 이 슬롯은 무리스(unleased)라 `--repo --slot`로 해소되지 않으므로 `--cwd <readonly 슬롯 절대경로>`로 핀한다.

```bash
# main-참조 기준면 = readonly 슬롯(detached·origin/main 추적). PM_ORCH_LIVE_RELEASE=1 필수.
PM_ORCH_LIVE_RELEASE=1 python3 .project_manager/tools/board.py livegate record --cwd <readonly 슬롯 절대경로>
#   readonly 슬롯 경로는 `pm-config status` 의 role=readonly 행에서 확인(`worktree` 서브커맨드는 add·prune-stale·remove 뿐).
```

record는 라이브 wave 전에 추가 리뷰 라운드 장부의 미해소 must-fix를 차단한다(rc=1·우회 플래그 없음). 차단되면 현재 티켓 fix의 기계 확인이 끝난 게이트만 `additional_reviewer.py --resolve-gate <게이트> --pm-verified`로 선언한다. 다른 티켓이나 근거 게이트로 잔여를 넘기는 처분은 없다. 상태는 `additional_reviewer.py --rounds-report`로 확인하며 `livegate check`도 같은 판정을 반복한다.

- 릴리즈 readonly 기준면은 기존 `--cwd <절대경로>`로 핀한다. 일반 task의 소유 작업공간에서 livegate를 기록하는 경우는 `--task <task> --repo <repo> --slot <N>`으로 실행 대상을 선택한다. task 귀속과 선택한 HEAD·브랜치·test_cmd를 결속한다. 두 경로를 섞거나 다중 슬롯에서 암묵적으로 고르지 않는다.
- 릴리즈 전 추가 리뷰어 교차검증(`additional_reviewer`)의 `--paths`도 같은 readonly 슬롯 worktree를 가리킨다.
- `PM_ORCH_LIVE_RELEASE=1`이 없으면 release wave가 skip되어 수집 N=0, record가 fail한다.
- board 출력 `release N/<pin> green ✓`의 N==pin을 눈으로 확인해 보고한다. 다르면 마커 소실·wrong-cwd이므로 fail로 릴리즈를 막는다.
- live tier는 **release 단일**이고 **3 하네스 실측**이다 — claude·opencode 는 wave 전 구간(부트스트랩~핸드오프·multi-repo·multi-user), codex 는 위임 완주와 relay 마커 정체성 2건으로 커버 깊이가 얕다. opencode 는 회사 기준 버전·격리 `--dir`·glm-5.2 로 돈다.

#### Windows 관리자 창 확인 (사람 확인)

공개 main push 승인 요청 전에 PM이 실제 Windows 데스크톱에서 같은 릴리즈 커밋의 코드 트리를 확인한다.
관리자 실행 의존성을 설치하고 해당 커밋에서 `manager/ui/dist`를 빌드한 뒤 코드 트리 루트에서 실행한다.
사용자 등록부를 사용하지 않고, 이번 확인 전용으로 아직 존재하지 않는 임시 `projects.json` 절대경로를 지정한다.

```powershell
python -m manager --smoke --registry "<이번 확인 전용 임시 projects.json 절대경로>"
```

창 표시와 자동 종료를 확인하고, Windows 버전·검증 커밋 SHA·실행 명령·종료 코드 `rc=0`·`smoke ok` 출력을 보고한다.
미실행 또는 실패 시 공개 main push 승인 요청을 중단한다. 수정으로 커밋이 바뀌면 같은 확인을 다시 실행한다.
이 소스 smoke 결과는 livegate 기록에 넣지 않는다. 공개 후 관리자 실행 파일 3종의 CI 확인도 별도로 수행한다.
새 기계 게이트·설정 키·우회 옵션은 추가하지 않는다.

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

   릴리즈 본문 파일은 설치 프롬프트 → 업데이트 프롬프트 → CHANGELOG `[X.Y.Z]` 절 순서로 작성한다.
   두 프롬프트는 각각 독립된 `text` 코드 블록으로 담고, CHANGELOG 해당 버전 절의 제목과 본문은 그대로 포함한다.
   예제의 `vX.Y.Z`를 실제 릴리즈 태그로 채우며 두 프롬프트의 태그와 CHANGELOG `[X.Y.Z]` 버전을 일치시킨다.
   과거 릴리즈의 특수 처방(실행 횟수·설정 정리 항목 등)은 고정하지 않고 해당 버전 업그레이드 노트를 따른다.

   설치 프롬프트:

   ```text
   project_manager vX.Y.Z로 새 PM 홈을 구성한다.
   1. https://github.com/sumin-ahn/project_manager 저장소를 vX.Y.Z 태그로 clone한다.
   2. 해당 태그의 ADOPT.md를 읽고 PM 홈 경로 <경로>에 적용한다.
   3. ADOPT.md의 첫 커밋까지 수행한다. 이후 작업 폴더 생성은 명령만 제시하고 사용자 승인을 받은 뒤 실행한다.
   4. 실패하면 우회하지 않고 출력을 그대로 보고한다.
   ```

   업데이트 프롬프트:

   ```text
   이 PM 홈의 project_manager 엔진을 vX.Y.Z로 갱신한다.
   1. 대상 vX.Y.Z의 CHANGELOG [X.Y.Z] 절에 있는 업그레이드 노트를 먼저 읽는다.
   2. ./pm-config.sh upstream show로 갱신 소스를 조회하고 로컬 경로라면 그 소스를 vX.Y.Z로 맞춘다.
   3. 해당 버전 업그레이드 노트 순서에 따라 ./pm-update.sh로 갱신한다.
   4. ./pm-config.sh sync-adapter-config --check의 rc=0을 확인한다. --accept가 필요하면 적용 전에 차이를 제시한다.
   5. 이 PM 홈에 적용한 항목과 결과를 보고한다. 실패하면 우회하지 않고 출력을 그대로 보고한다.
   ```

   Windows 프롬프트에서는 `.sh`를 `.cmd`로 바꾼다. 완성한 릴리즈 본문 파일을 `--notes-file`에 전달한다.

   ```bash
   gh release create vX.Y.Z --notes-file <완성한 릴리즈 본문 파일> --verify-tag
   ```

   gh 미인증이면 생략하지 말고 사용자에게 넘기며 **릴리즈 미완료**로 명시한다.

4. **관리자 실행 파일 3종 도착 확인**(손 업로드가 아니라 워크플로가 올린다). 3번의
   `gh release create`가 `release-manager.yml`(`on: release: types: [published]`)을 깨우고, 그
   워크플로가 세 OS에서 `manager/build.py` → `--smoke`(ubuntu는 `xvfb-run -a`) →
   `gh release upload vX.Y.Z <파일> --clobber`를 돈다.

   ```bash
   gh run list --workflow release-manager.yml --limit 3   # 그 릴리즈로 깨어난 실행
   gh run view <run-id>                                   # ubuntu·windows·macos 세 job 성공
   gh release view vX.Y.Z                                 # 자산 pm-manager-{linux,windows,macos}-* 3종
   ```

   `--smoke` 세 OS 결과는 livegate 기록에 넣지 않는다(그 스키마는 `pytest -m release` 축이다).
   job 성공·실패가 기계 기록이고 이 단계는 그 결과와 자산 3종을 확인한다. job이 red면 릴리즈는
   자산 없이 열린 상태이므로, 사유를 고친 뒤 `gh run rerun --failed <run-id>`로 같은 릴리즈에
   다시 붙인다.

5. **릴리즈 완결 확인**:

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
