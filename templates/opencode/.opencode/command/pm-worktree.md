---
name: pm-worktree
description: "worktree/submodule 운영중 관리 — submodule 을 dev 브랜치로 지정(pool selective resync 로부터 보호)·drift 난 detached submodule 을 pin 으로 수동 재동기·슬롯 기준점(base) 사용자 명시 기록(set-base)·슬롯 git 구성 조회(status·단일/일괄·submodule pin/drift·dirty)·슬롯 base rebase(단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신·자동 rebase 없음)·readonly 공유 슬롯 갱신(refresh)·슬롯 lease git 스냅 명시 재동기(record — 0단계 diverged 정당 판단 시)·슬롯 브랜치 전환 + 스냅 재기록 원자(switch — 0단계 main-참조 해소). backbone CLI .project_manager/tools/worktree_pool.py (dev/sync/set-base/status/rebase/refresh/record/switch) thin wrapper. Triggers: 'submodule dev 지정', 'submodule 작업 중 선언', 'worktree submodule drift 재동기', 'worktree 브랜치 관리', '슬롯 기준점 지정', 'set-base', 'worktree status', '슬롯 rebase', 'worktree rebase', '슬롯 base 변경', 'readonly 슬롯 갱신', 'refresh', '슬롯 재동기', 'record', '슬롯 브랜치 전환', 'worktree switch', 'switch', 'main-참조 해소', 'pm-worktree'."
audience: pm-internal
---

# /pm-worktree — worktree/submodule 운영중 관리

{{PROJECT_NAME}} 슬롯의 worktree/submodule 운영중 관리용 **pm-internal** 스킬. backbone =
`.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`set-base`/`status`/`rebase`/`refresh`/`record`/`switch`);
비즈니스 로직 없는 thin wrapper다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](../../.claude/skills/pm-worktree/references/operational-details.md)를 해당 상황에서 읽는다.

## 청중 (audience)

**pm-internal** — PM 에이전트가 세션 중 자동 invoke(dev-delegate 류)하는 운영중-관리 스킬이며
셋업(pm-env·user-entrypoint) 확장이 아니다.

## 사용 시점 (trigger)

- submodule 을 직접 고치기 **전**, 먼저 `dev` 로 지정해 브랜치 전환·부트스트랩 selective resync가
  detached pin으로 낚아채지 않게 한다.
- detached(consume) submodule 이 superproject pin과 어긋나면 브랜치 전환 없이 `sync`로 수동
  재동기한다. 상태 확인은 부트스트랩 `### 슬롯 상태`가 surface한다.
- 부트스트랩 0단계가 v1.3.0 이전 슬롯에 "기준점 미기록 — drift 감지 비활성"과 후보를 표시하면,
  사용자가 고른 base를 `set-base`로 기록한다. 자동 추론하지 않는다.
- `status`로 role·base·branch·head·base 대비 N behind·submodule pin/drift·dirty를 조회한다.
  단일(`<slot>`), 일괄(`--task <이름>`), 무인자(내 task 전 슬롯)를 지원하며, base 미기록 시 behind는
  `-`(계산 불가)다.
- 원할 때만 `rebase`로 기록된 base 또는 `--onto <branch>` 최신에 rebase한다. 단일(`<slot>`),
  일괄(`--task <이름>`), task 명의 단일(`<slot> --task <이름>`)을 지원하며 자동 rebase는 없다.
- research 전용 readonly 공유 슬롯은 `refresh`로 released 최신 tip에 갱신한다. fetch 후 detached
  HEAD를 옮기며, dirty면 조용히 reset하지 않고 거부한다.
- 부트스트랩 0단계가 보호 브랜치 직접 checkout 또는 보호 브랜치 원격 origin-추적으로 진입을
  거부하면 안내된 `switch <slot> <branch>`로 작업 브랜치로 전환한다. 전환과 스냅 재기록이 한 호출에
  일어나 다시 diverged로 막히지 않는다.

## 정체성 / 슬롯

이 명령들은 대상 슬롯에 실 git 부작용(`checkout`·`submodule update`·`rebase`·base 기록)을 낸다.
오타깃 방지를 위해 `dev`/`sync`에 `--repo <repo> --slot <N>`을 명시한다(예:
`--repo project_manager --slot 1`). 생략하면 cwd·세션 leased 슬롯으로 해소하지만, 미해소(0개)·
모호(≥2)면 침묵 no-op 없이 rc 1로 실패한다.

`set-base`/`status`/`rebase`/`refresh`/`record`/`switch`는 위치인자 `<slot>`으로 임의 pool 슬롯을
직접 지정한다. `status`/`rebase`의 `--task <이름>`은 그 task의 전 슬롯을 지칭한다. `rebase <slot>
--task <이름>`은 대상 자원(`<slot>`)과 소유 명의(`--task`)를 함께 주는 단일 연산이므로 두 인자는
배타가 아니다. submodule 경로·슬롯은 형식/목록 검증하며 절대경로·`..` 등 슬롯 경계 밖은 거부한다.

## 실행

공유 루트(`.project_manager` 있는 곳)에서 실행한다:

```bash
# submodule 을 dev 브랜치로 지정 → 이후 selective resync 가 skip (dev 작업 보호)
python3 .project_manager/tools/worktree_pool.py dev <submodule_path> <branch> --repo <repo> --slot <N>

# 현재 슬롯 submodule 수동 재동기 (detached=pin 재동기·on-branch=skip·dirty=skip+경고)
python3 .project_manager/tools/worktree_pool.py sync --repo <repo> --slot <N>

# 슬롯 기준점(base) 사용자 명시 기록 (미기록 슬롯 해소 → 이후 drift 감지 작동·자동 추론 금지)
python3 .project_manager/tools/worktree_pool.py set-base <slot> <branch>[@<commit>]

# 슬롯 git 구성 조회 (role·base·branch·head·N behind·submodule pin/drift·dirty·단일/일괄)
python3 .project_manager/tools/worktree_pool.py status [<slot>]              # 단일(무인자=내 task 전 슬롯)
python3 .project_manager/tools/worktree_pool.py status --task <이름>          # 그 task 보유 전 슬롯 일괄

# 슬롯 base rebase (선-검사·충돌 그대로+loud·성공 시 장부 원자 갱신·자동 rebase 없음)
python3 .project_manager/tools/worktree_pool.py rebase <slot> [--onto <branch>]        # 단일
python3 .project_manager/tools/worktree_pool.py rebase --task <이름> [--onto <branch>]  # 일괄(그 task 전 슬롯)

# readonly 공유 슬롯 갱신 (fetch → detached HEAD 이동 + submodule 재동기·dirty=거부+loud)
python3 .project_manager/tools/worktree_pool.py refresh <slot> [--onto <branch>]

# 슬롯 도착 스냅(lease.git) 명시 재동기 (live 로 재기록·base 보존·0단계 diverged 정당 판단 시)
python3 .project_manager/tools/worktree_pool.py record <slot>

# 슬롯 브랜치 전환 + 장부 스냅 재기록 (원자·base 보존·보호브랜치 거부·0단계 main-참조 해소)
python3 .project_manager/tools/worktree_pool.py switch <slot> <branch>
```

- `<submodule_path>` = 슬롯 worktree 상대 경로(=`git submodule status` 표기, 예 `vendor/sub`).
- `dev`: submodule을 on-branch(dev 역할)로 만들어 이후 브랜치 전환·부트스트랩 selective resync가
  **skip**하게 한다(detached pin 전환 방지).
- `sync`: selective 재동기를 수동 실행한다. dev(on-branch)·dirty submodule은 **skip**해 보호하고,
  detached & clean만 pin으로 재동기한다.
- `set-base`: `<slot>`은 위치인자(예 `work/proj_1` 또는 접두 생략 `proj_1`)다.
  `<branch>[@<commit>]`은 기준 브랜치(예 `origin/main`)와 선택적 커밋이며, 커밋 생략 시 브랜치 tip을
  쓴다. **자동 추론·`merge-base` 추측 금지**: 사용자가 명시한 브랜치만 기록한다. 해소 불가
  ref(오타·미fetch)는 slot HEAD로 폴백하지 않고 rc 1로 거부하므로 실재 ref를 지정하거나 fetch 후
  재시도한다. 부트스트랩 0단계가 미기록 슬롯에 후보(예 "`origin/main`(merge-base `df10dc6`)")를
  제시하면 사용자가 고른 하나만 기록한다.
- `status`: `<slot>` 단일, `--task <이름>` 일괄, 둘 다 생략 시 내 task(세션) 전 슬롯. 슬롯별
  role(work/readonly)·base·branch·head·base 대비 N behind·submodule pin/drift(⚠=drift/uninitialized)·
  dirty를 surface한다. base 미기록 시 behind는 `-`이며 추론하지 않는다.
- `rebase`: `<slot>`(단일), `--task <이름>`(그 task 전 슬롯 일괄), `rebase <slot> --task <이름>`(그 task
  명의 단일). `<slot>` 단독은 세션 명의다. `--onto <branch>` 생략 시 기록된 base.branch 최신을
  사용하며, 미기록이면 거부하므로 `set-base` 또는 `--onto`로 명시한다. 슬롯마다 독립 처리해 일괄의
  한 충돌이 나머지를 막지 않으며 끝에 성공/스킵/충돌을 요약한다.
  - **선-검사 3종(스킵+loud)**: 호출 명의(세션 또는 `--task`)로 leased한 슬롯이 아님, dirty,
    rebase 진행 중. readonly도 mutation 불가라 스킵한다. 타 명의 슬롯은 계속 스킵하고, 보유자가
    등록 task면 메시지가 실제 `--task <이름>` 해소를 안내한다.
  - ⚠ 명의는 호출자의 자칭이다. `--task <남의 task>`도 통과하며 `release --task`의 소유검사·세션
    축(`PM_SESSION_NAME`)과 같은 모델이다. 오조작 방지 가드이지 보안 경계가 아니다.
  - **충돌**: 임의 abort 없이 상태를 그대로 두고 fail-loud한다. 사용자가 슬롯에서 충돌 해결 후
    `git rebase --continue`하거나 `git rebase --abort`한다. 미완이므로 장부 base는 갱신하지 않으며,
    다음 부트스트랩 0단계가 "rebase 진행 중"으로 감지·안내한다.
  - **성공**: 장부의 base.commit=새 base tip·head=새 tip·recorded_at을 원자 갱신한다.
  - **자동 rebase 없음**: 사용자 요청 때만 실행한다.
- `refresh`: **readonly 공유 슬롯 전용**. fetch 후 detached HEAD를 `--onto <branch>` 또는 기록된
  base.branch 최신 tip으로 옮기고 submodule을 재동기해 옛 gitlink pin 잔존에 따른 stale+dirty
  자가 잠금을 막는다. ref 해소 규칙: `--onto` 명시 = 준 ref 그대로(로컬 브랜치면 로컬 tip·자동 대체
  없음·미해소는 loud 거부), 무인자 = 기록된 base.branch 의 `origin/<branch>` 우선(부재 시 로컬 폴백).
  성공 메시지가 실제 해소된 ref 와 sha 를 찍는다. 이동 tip을 base.commit으로 재기록한다. dirty면 "누군가 여기 썼다"는
  신호이므로 reset하지 않고 loud 거부한다. 기준 미해소 또는 non-readonly 대상은 rc 1이다.
  readonly 슬롯의 `set-base`/`rebase`/`dev`/`sync`·`release`/바인딩(`/pm-bootstrap --slot`)은
  엔진이 거부하며 갱신은 `refresh`만 허용한다.
- `record`: `<slot>` 위치인자. 현재 live branch·head·recorded_at으로 `lease.git` 도착 스냅만 명시
  재기록하고 base는 보존한다(base는 `set-base`/`rebase`만 변경). 부트스트랩 0단계의
  "기록↔live diverged"가 **정당한 의도적 전환·릴리즈 진행 등이라고 사용자가 판단했을 때만** 쓴다.
  엔진은 자동 재기록하지 않는다. 장부 미등록 슬롯·스냅 불가(슬롯 경로 부재 등)는 조용한 무변경 없이
  rc 1이다. 아직 전환 전이면 `record` 대신 `switch`로 전환과 재기록을 함께 한다.
- `switch`: `<slot> <branch>`로 슬롯 브랜치를 전환하고 같은 호출에서 장부 branch·head·recorded_at을
  원자 재기록하며 base는 보존한다. 기존 브랜치는 `checkout --no-recurse-submodules <b>`, 미존재
  브랜치는 비파괴 `-b <b>`로 전환한다. 어느 경로도 `-B`(create-or-reset)를 쓰지 않아 기존 브랜치
  ref를 리셋하지 않는다. `sync`/`alloc`과 같은 프리미티브
  (`--no-recurse-submodules` + selective resync)를 사용해 on-branch(dev)는 보호하고 detached는 새
  pin으로 재동기하며 dirty는 skip+경고한다. raw `git switch`는 이 보호와 장부 기록을 건너뛰므로
  **손-git을 쓰지 마라**.

  다음 선검사 중 하나라도 걸리면 **부작용 0·rc 1 loud**:
  - 보호목록(areas.md `protected`) 브랜치(`main` 등)로의 전환.
  - 보호브랜치 원격을 추적하는 기존 브랜치. 다음 0단계가 다시 main-참조로 막으므로 upstream 없는
    새 작업 브랜치(`switch <slot> <새-브랜치명>`)로 전환한다. 자기 feature 추적(`origin/a5`)은 허용.
  - 다른 worktree가 이미 checkout 중인 브랜치(통합 브랜치 `task/main` 등). 옛 `-B`(create-or-reset)
    폴백 결함에서는 전환 시 그 브랜치 ref가 이 슬롯 HEAD로 리셋됐다 — 지금은 선-검사가 부작용
    없이 거부하며 우회 플래그는 없다. 보유 worktree 경로를 함께 보고한다.
  - dirty, rebase 진행 중(먼저 continue/abort), readonly 슬롯, 장부 미등록 슬롯.
  - 부적합 ref명(`git check-ref-format --branch`) 또는 D/F 충돌(브랜치 `task`가 있으면
    `task/main`은 `cannot lock ref`; 접두 부모 ref를 미리 검사).
  - 새 브랜치 자리에 remote-tracking ref(`origin/main`)나 태그명을 주는 모호 인자. detached HEAD와
    보호목록 검사를 우회할 수 있으므로 새 로컬 브랜치명을 준다. 원격을 따라가려면 로컬 브랜치를 만든
    뒤 `record`로 스냅 정합한다.

  브랜치 인자는 git의 실 브랜치명으로 정규화한 뒤 검사·전환·기록에 일관 적용한다. revspec
  (`@{-1}`)도 실명으로 확장하고, 그 실명이 보호목록이면 거부한다. 원문과 다르게 해소되면 "입력
  `@{-1}` → `main` 으로 해소됨"을 loud 표시한다. 전환 후 장부 IO/권한/락 오류 등으로 스냅 재기록이
  실패하면 성공으로 위장하지 않고 loud 안내하며, `record <slot>`으로 스냅만 맞춘다. 그대로 두면
  다음 0단계가 diverged로 막힌다.
