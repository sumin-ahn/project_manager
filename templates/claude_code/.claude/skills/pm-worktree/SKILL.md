---
name: pm-worktree
description: "worktree/submodule 운영중 관리 — submodule 을 dev 브랜치로 지정(pool selective resync 로부터 보호)·drift 난 detached submodule 을 pin 으로 수동 재동기·슬롯 기준점(base) 사용자 명시 기록(set-base)·슬롯 git 구성 조회(status·단일/일괄·submodule pin/drift·dirty)·슬롯 base rebase(단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신·자동 rebase 없음)·readonly 공유 슬롯 갱신(refresh)·슬롯 lease git 스냅 명시 재동기(record — 0단계 diverged 정당 판단 시)·슬롯 브랜치 전환 + 스냅 재기록 원자(switch — 0단계 main-참조 해소). backbone CLI .project_manager/tools/worktree_pool.py (dev/sync/set-base/status/rebase/refresh/record/switch) thin wrapper. Triggers: 'submodule dev 지정', 'submodule 작업 중 선언', 'worktree submodule drift 재동기', 'worktree 브랜치 관리', '슬롯 기준점 지정', 'set-base', 'worktree status', '슬롯 rebase', 'worktree rebase', '슬롯 base 변경', 'readonly 슬롯 갱신', 'refresh', '슬롯 재동기', 'record', '슬롯 브랜치 전환', 'worktree switch', 'switch', 'main-참조 해소', 'pm-worktree'."
audience: pm-internal
---

# /pm-worktree — worktree/submodule 운영중 관리

> {{PROJECT_NAME}} 슬롯의 worktree/submodule 을 세션 중 관리하는 pm-internal 스킬 — 어떤
> submodule 을 직접 고칠 때 **dev 브랜치로 지정**해 pool 의 selective resync(브랜치 전환·
> 부트스트랩)로부터 보호하고, drift 난 detached(consume) submodule 을 pin 으로 **수동 재동기**하며,
> 슬롯 **기준점(base)을 사용자 명시로 기록**(`set-base`)하고, 슬롯 git 구성을 **조회**(`status`·단일/
> 일괄·submodule pin/drift·dirty)하며, 슬롯 base 를 **rebase**(단일/일괄·선-검사·충돌 그대로+loud·
> 장부 원자 갱신)하고, readonly 공유 슬롯을 released 최신으로 **갱신**(`refresh`)하고, 슬롯 lease git 스냅을 **명시 재동기**(`record` — 부트스트랩 0단계 diverged 를 사용자가 정당 판단했을 때·T-0391)하며, 슬롯 브랜치를 **전환+스냅 재기록 원자**로 옮긴다(`switch` — 0단계 main-참조 해소·T-0414). backbone =
> `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`set-base`/`status`/`rebase`/`refresh`/`record`/`switch`). 비즈니스
> 로직 0 — 엔진 CLI 호출 thin wrapper (ADR-0049 명령어化 4요소·ADR-0051 live-HEAD 역할모델).

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 세션 중 자동 invoke(dev-delegate 류). 사용자가 자연어로
"이 submodule 은 내가 작업 중"·"submodule drift 정리해"·"이 슬롯 base 는 origin/main"·"슬롯 최신
main 으로 rebase 해" 라고 지시하면 PM 이 이 스킬을 부른다. 셋업(pm-env·user-entrypoint)의 확장이
아니라 **운영중-관리** 스킬 — 청중이 다르다(ADR-0049).

## 사용 시점 (trigger)

- **submodule 을 직접 고치기 전** — 슬롯 worktree 안 어떤 submodule 에서 작업하려 할 때,
  먼저 그 submodule 을 dev 로 지정해 pool 이 detached pin 으로 낚아채지 않게 한다
  ("submodule vendor/x 를 dev 로").
- **drift 재동기** — 브랜치 전환 없이 detached(consume) submodule 이 superproject pin 과 어긋났을
  때 수동 재동기한다 ("submodule drift 재동기해"). (상태 *확인*은 부트스트랩의 `### 슬롯 상태`
  절이 이미 surface 한다 — T-0276.)
- **기준점(base) 지정** — 부트스트랩 0단계가 "기준점 미기록 — drift 감지 비활성" 을 loud 표시하고
  후보를 제시하면(v1.3.0 이전 슬롯), 사용자가 고른 기준을 `set-base` 로 기록한다("이 슬롯 base 는
  origin/main"). 그때부터 drift 감지가 작동한다(자동 추론 없음·사용자 결정·결정 ⑪).
- **슬롯 git 구성 조회** — 슬롯의 role·base·branch·head·**base 대비 N behind**·submodule pin/drift·
  dirty 를 `status` 로 본다(손-git 조합 불요). 단일(`<slot>`)·일괄(`--task <이름>`·무인자=내 task 전
  슬롯). 기준점 미기록이면 "base 대비 behind" 는 `-`(계산 불가).
- **슬롯 base rebase** — 슬롯을 기록된 base(또는 `--onto <branch>`) 최신으로 rebase 한다("이 슬롯
  main 최신으로 rebase 해"). 단일(`<slot>`)·일괄(`--task <이름>`·그 task 보유 전 슬롯)·**task 명의
  단일**(`<slot> --task <이름>`). 자동 rebase 없음 — **원할 때만**(결정 ⑤ 정신).
- **readonly 공유 슬롯 갱신** — research 전용 read-only 슬롯(⑬·`worktree add --readonly` 로 생성)을
  released 최신 tip 으로 `refresh` 한다("readonly 슬롯 최신으로 갱신해"). fetch → detached HEAD 이동.
  dirty(누군가 씀·신호)면 거부(조용히 reset 안 함).
- **0단계 main-참조 해소** — 부트스트랩 0단계가 "슬롯이 보호 브랜치를 직접 checkout / 보호 브랜치
  원격을 origin-추적" 이라며 진입을 거부하면(T-0360), 그 안내가 제시하는 `switch <slot> <branch>` 로
  작업 브랜치로 빠져나온다("이 슬롯 작업 브랜치로 전환해"). 전환과 장부 스냅 재기록이 **한 호출에**
  일어나 재진입이 diverged 로 다시 막히지 않는다(T-0414).

## 정체성 / 슬롯

이 스킬의 커맨드는 **대상 슬롯**에 실 git 부작용(`checkout`·`submodule update`·`rebase`·base 기록)을
낸다 — 정체성(에이전트 맥락)을 도구에 **명시 전달**해 오타깃을 막는다. `dev`/`sync` 는 `--repo <repo>
--slot <N>` 을 명시한다(예: `--repo project_manager --slot 1`) — 생략하면 cwd·세션 leased 슬롯으로
해소하나, 미해소(0개)·모호(≥2)면 rc 1 로 명확히 실패한다(침묵 no-op 아님). `set-base`/`status`/
`rebase`/`refresh`/`record`/`switch` 는 대상 슬롯을 **위치인자 `<slot>`** 으로 직접 지정한다(임의 슬롯 pool 관리 — 자기
세션 슬롯이 아닐 수 있음). `status`/`rebase` 는 `--task <이름>` 으로 그 task 보유 전 슬롯을 일괄
지칭한다. **`rebase` 의 `--task` 는 소유 명의 축도 겸한다**(T-0416) — `rebase <slot> --task <이름>` 은
그 task 명의로 **단일** 슬롯을 rebase 한다(위치인자와 배타 아님). 이는 실행-위치 핀 혼합이 아니라
대상 자원(`<slot>`) + 소유 명의(`--task`)인 자원 연산 예외다. submodule 경로·슬롯은 형식/목록
검증을 거쳐 슬롯 경계 밖(절대경로·`..`)은 거부된다.

## ⚠ rebase 선행조건 (⑳ — 활성 위임 중 금지)

**활성 백그라운드 위임(dev 서브에이전트)이 돌고 있는 슬롯은 rebase 하지 마라.** 서브에이전트는
하네스 안 프로세스라 엔진이 못 본다(기계 신호 부재·⑭의 정직한 한계) — rebase 가 그 슬롯의 working
tree 를 옮기면 위임 중 작업이 깨진다([[parallel-dev-shared-tree-clobber]] 게이트-변형). PM 은
**실행 전 그 슬롯에 활성 위임이 없는지 확인**한다(대여 슬롯을 dev 위임에 쓰는 중이면 위임 종료 후
rebase). 엔진은 dirty·rebase-진행중은 기계로 스킵하지만 *활성 위임*은 명문화된 사람-확인이다.

## 실행

공유 루트(`.project_manager` 있는 곳)에서 실행한다:

```bash
# ① submodule 을 dev 브랜치로 지정 → 이후 selective resync 가 skip (dev 작업 보호)
python3 .project_manager/tools/worktree_pool.py dev <submodule_path> <branch> --repo <repo> --slot <N>

# ② 현재 슬롯 submodule 수동 재동기 (detached=pin 재동기·on-branch=skip·dirty=skip+경고)
python3 .project_manager/tools/worktree_pool.py sync --repo <repo> --slot <N>

# ③ 슬롯 기준점(base) 사용자 명시 기록 (미기록 슬롯 해소 → 이후 drift 감지 작동·자동 추론 금지)
python3 .project_manager/tools/worktree_pool.py set-base <slot> <branch>[@<commit>]

# ④ 슬롯 git 구성 조회 (role·base·branch·head·N behind·submodule pin/drift·dirty·단일/일괄)
python3 .project_manager/tools/worktree_pool.py status [<slot>]              # 단일(무인자=내 task 전 슬롯)
python3 .project_manager/tools/worktree_pool.py status --task <이름>          # 그 task 보유 전 슬롯 일괄

# ⑤ 슬롯 base rebase (선-검사·충돌 그대로+loud·성공 시 장부 원자 갱신·자동 rebase 없음)
python3 .project_manager/tools/worktree_pool.py rebase <slot> [--onto <branch>]        # 단일
python3 .project_manager/tools/worktree_pool.py rebase --task <이름> [--onto <branch>]  # 일괄(그 task 전 슬롯)

# ⑥ readonly 공유 슬롯 갱신 (fetch → detached HEAD 이동 + submodule 재동기·dirty=거부+loud·⑬)
python3 .project_manager/tools/worktree_pool.py refresh <slot> [--onto <branch>]

# ⑦ 슬롯 도착 스냅(lease.git) 명시 재동기 (live 로 재기록·base 보존·0단계 diverged 정당 판단 시)
python3 .project_manager/tools/worktree_pool.py record <slot>

# ⑧ 슬롯 브랜치 전환 + 장부 스냅 재기록 (원자·base 보존·보호브랜치 거부·0단계 main-참조 해소)
python3 .project_manager/tools/worktree_pool.py switch <slot> <branch>
```

- `<submodule_path>` = 슬롯 worktree 상대 경로(= `git submodule status` 표기·예 `vendor/sub`).
- `dev`: 그 submodule 을 on-branch(dev 역할)로 만든다 — 이후 브랜치 전환·부트스트랩의 selective
  resync 가 그 submodule 을 **skip**(detached pin 으로 안 낚아챈다·ADR-0051 크럭스 A 보호).
- `sync`: pool selective 재동기(T-0275)를 수동 트리거한다. **dev(on-branch)·dirty submodule 은
  자동 보호**(skip) — 되돌릴 수 없는 pin 재동기는 detached & clean submodule 에만 일어난다.
- `set-base`: `<slot>` 은 **위치인자**(예 `work/proj_1` 또는 접두 생략 `proj_1`) — dev/sync 의
  `--repo/--slot` 과 달리 대상 슬롯을 직접 지정한다(임의 슬롯 pool 관리). `<branch>[@<commit>]` =
  기준 브랜치(예 `origin/main`), 커밋 생략 시 그 브랜치 tip. **자동 추론 절대 안 함** — 사용자가
  명시한 브랜치만 base 로 기록한다(`merge-base` 추측 금지·틀려도 조용한 base 위 drift 감지 차단·
  결정 ⑪). **해소 불가 ref(오타·미fetch)는 rc 1 로 거부**된다 — slot HEAD 로 조용히 폴백해
  오기록하지 않으니, 실재하는 브랜치/커밋을 지정하거나 fetch 후 재시도한다. 미기록 슬롯이 있어야
  부트스트랩 0단계가 후보(예 "`origin/main`(merge-base `df10dc6`)")를 제시한다 — 사용자가 그중
  하나를 골라 이 명령으로 기록한다.
- `status`: `<slot>` 단일·`--task <이름>` 일괄·무인자 = 내 task(세션) 전 슬롯(둘 다 생략). 슬롯별
  role(work/readonly)·base·branch·head·**base 대비 N behind**·submodule pin/drift(⚠=drift/
  uninitialized)·dirty 를 surface. "base 대비 N behind" 는 기준점이 있어야 계산된다 — 미기록이면
  `-`(계산 불가·자동 추론 금지).
- `rebase`: `<slot>`(단일)·`--task <이름>`(그 task 보유 전 슬롯 일괄)·**`<slot> --task <이름>`**(그 task
  명의로 단일·T-0416). `--task` 는 **선택 축 + 소유 명의 축**이라 위치인자와 배타가 아니다 — task 명의
  슬롯을 단일 지정으로 rebase 하려면 이 형태를 쓴다. `<slot>` 단독은 종전대로 세션 명의. `--onto
  <branch>` 생략 = 기록된 base.branch 최신으로 rebase(**미기록이면 거부** — `set-base` 또는 `--onto`
  로 기준 명시·추론 금지·결정 ⑪). **슬롯마다 독립 처리**(일괄에서 한 충돌이 나머지를 안 막음) + 끝에
  성공/스킵/충돌 요약:
  - **선-검사 3종(스킵 + loud)**: **내 명의(세션 또는 `--task` 로 준 task) leased 슬롯이 아님**
    (T-0416 — 축 확장이지 검사 제거가 아니다·타 명의는 계속 loud 스킵이고 보유자가 등록 task 면
    스킵 문구가 `--task <이름>` 해소를 실값으로 안내한다) / dirty(rebase 는 clean 전제) /
    rebase 진행 중. (readonly 공유 슬롯도 mutation 불가라 스킵.)
  - ⚠ **명의는 호출자가 밝히는 것**(자칭) — `--task <남의 task>` 를 주면 통과한다. `release --task`
    의 소유검사(F3)·세션 축(`PM_SESSION_NAME`)과 **같은 모델**이다. 이 가드는 **오조작 방지**이지
    타 PM 을 막는 보안 경계가 아니다(내부 리뷰 지적·정직 서술).
  - **충돌** = **그 상태 그대로 두고 fail-loud** — 엔진이 임의 abort 하지 않는다(해소는 사용자:
    슬롯에서 `git rebase --continue` 충돌 해결 후 또는 `git rebase --abort` 취소). 장부 base **미갱신**
    (미완) → 다음 부트스트랩 0단계가 "rebase 진행 중" 으로 감지·안내한다.
  - **성공** = 장부 원자 갱신(base.commit=새 base tip · head=새 tip · recorded_at) — 손-git 은 장부를
    못 갱신해 기록이 즉시 stale 되던 문제를 조작과 원자적으로 묶는다.
  - **자동 rebase 없음** — 원할 때만(결정 ⑤ 정신: 엔진이 알아서 막 하지 않는다).
- `refresh`: **readonly 공유 슬롯(⑬) 전용** — fetch → detached HEAD 를 기준(`--onto <branch>` 또는
  기록된 base.branch) 최신 tip 으로 이동하고 **submodule 을 재동기**한다(gitlink 옛 pin 잔존→stale+dirty
  자가 잠금 방지). 이동한 tip 을 base.commit 으로 재기록한다(기준면=released base 이동). **dirty 면
  거부 + loud**(read-only 슬롯의 dirty 는 "누군가 여기 썼다"는 신호 — 조용히 reset 하지 않는다·사용자가
  보고 판단). 기준 미해소(추론 금지)·대상이 readonly 가 아니면 rc 1. **lease/mutation 거부**: readonly
  슬롯에 `set-base`/`rebase`/`dev`/`sync`·`release`/바인딩(`/pm-bootstrap --slot`)은 엔진이
  거부한다(문서 검증 기준면·무소유 공유 자산·갱신은 refresh 만·⑬·§F11).
- `record`: `<slot>` 위치인자 — 슬롯 lease 의 **도착 스냅**(`lease.git` 의 branch·head·recorded_at)을
  현재 live 상태로 **명시 재기록**한다(base 는 **보존** — 기준점은 `set-base`/`rebase` 로만 바뀐다).
  부트스트랩 0단계가 "기록↔live diverged" 로 진입을 막았을 때, 그 차이가 **정당**(내가 의도한 브랜치
  전환·릴리즈 진행 등)이라고 **사용자가 판단했을 때만** 쓰는 재동기 진입이다(감지=기계·해소=사용자·
  엔진이 자동 재기록하지 않는다·T-0391). 장부에 없는 슬롯·스냅 불가(슬롯 경로 부재 등)는 rc 1 로
  명시 실패한다(조용한 무변경 없음). 아직 **전환하기 전**이라면 `record` 가 아니라 아래 `switch` 를
  써라 — 전환과 재기록을 한 번에 해서 diverged 자체가 안 생긴다.
- `switch`: `<slot>` 위치인자 + `<branch>` — 슬롯 브랜치를 옮기고 **같은 호출 안에서** 장부 도착
  스냅(branch·head·recorded_at)을 재기록한다(**원자**·base 는 보존·`record` 와 동형). 기존 브랜치면
  `checkout --no-recurse-submodules <b>`·미존재면 **비파괴 생성** `-b <b>` 로 전환한다(브랜치 전환은 `sync`/
  `alloc` 과 **같은 프리미티브**를 탄다 — `--no-recurse-submodules` + selective resync 로 on-branch(dev)
  submodule 보호·detached 는 새 pin 재동기·dirty 는 skip+경고·ADR-0051 크럭스 A). **손-git 을 쓰지
  마라** — raw `git switch` 는 그 submodule 보호도 건너뛰고 장부
  스냅을 안 남겨 바로 다음 부트스트랩 0단계가 "기록↔live diverged" 로 막는다(0단계 main-참조 해소가
  구조적으로 왕복 2회를 강제하던 문제·T-0414). 선-검사(하나라도 걸리면 **부작용 0**·rc 1 loud):
  - **보호목록 브랜치로의 전환 거부** — 이 커맨드는 main-참조를 *벗어나는* 전환용이라 `main` 등으로
    들어가는 전환은 목적과 정반대다(보호목록 = areas.md `protected`·T-0076).
  - **보호브랜치 원격을 추적하는 기존 브랜치 거부** — 전환은 되지만 그 슬롯이 `origin/main` 을
    origin-추적하는 상태라 다음 0단계가 **다시 main-참조로 막는다**(§F9 축 2). upstream 없는 새
    작업 브랜치로 전환하라(`switch <slot> <새-브랜치명>`). 자기 feature 추적(`origin/a5`)은 정상 통과.
  - **dirty**(전환이 미커밋 WIP 를 흔든다) · **rebase 진행 중**(먼저 continue/abort 로 해소) ·
    **readonly 슬롯**(⑬ mutation 불가) · **장부 미등록 슬롯**(스냅 기록 대상 아님).
  - **부적합 ref 명**(`git check-ref-format --branch` — 판정은 git 자신) · **D/F 충돌**(브랜치 `task`
    가 있으면 `task/main` 은 `cannot lock ref` 로 실패 — 접두 부모 ref 를 미리 본다).
  - **모호 인자 거부** — 새 브랜치를 만드는 자리에 **remote-tracking ref(`origin/main`)나 태그명**을
    주면 거부한다. 그 이름은 detached HEAD 이동을 부르고 보호목록(`main`)과 문자열이 달라 검사를
    우회한다 — **새 로컬 브랜치명**을 줘라(원격을 따라가려면 로컬 브랜치를 만든 뒤 `record` 로 스냅 정합).
  브랜치 인자는 **git 이 해석할 실 브랜치명으로 정규화**된 뒤 검사·전환·기록에 일관 적용된다 —
  revspec(`@{-1}`=이전 브랜치)은 실명으로 확장되므로, 그 실명이 보호목록이면 거부된다(원문
  문자열로 비교해 `@{-1}` 이 `main` 으로 전환되던 우회 폐쇄). 원문과 다르게 해소되면 "입력
  `@{-1}` → `main` 으로 해소됨" 을 loud 로 알린다(조용한 오전환 없음).
  전환은 됐는데 스냅 재기록이 실패하면(장부 IO/권한/락 오류 포함) 성공 위장 없이 loud 하게 알리고
  위 `record <slot>`(⑦)으로 스냅만 따로 맞추도록 안내한다 — 그대로 두면 다음 0단계가 diverged 로 막는다.

## 결정 (모델 · ADR-0051 · ADR-0060 · ADR-0061)

- submodule 역할 = **live git HEAD 판별**(무스키마·별도 장부 없음): on-branch=dev(보호)·
  detached=consume(재동기 대상).
- 전역 `submodule.recurse=true` 는 안 쓴다(dev 브랜치를 detached pin 으로 파괴하던 크럭스 A) →
  **selective**. dirty 가드로 미커밋 작업을 보호한다.
- 슬롯 기준점(base) = **rebase 로만 바뀌는 기대 축**(ADR-0060·live 표시와 별개). **미기록이면 추론
  금지·사용자 질의**(엔진=상태 surface·PM=확인·사용자=결정·결정 ⑪) — `set-base` 로 명시 지정한다.
  `merge-base` 추측은 rebase 이력·다중 후보에서 조용히 틀리고, 그 가짜 base 위에서 drift 감지가 돌면
  무의미해진다.
- rebase 는 그 기준-gate 를 소비한다 — 기준 없으면 거부·`--onto` 명시 시 그 기준으로 진행하되 base
  기록은 **rebase 성공 시에만**(충돌 시 미기록 — onto 를 미리 기록하면 충돌/abort 후 장부가 거짓 base 를
  주장한다). 충돌은 엔진이 abort 하지 않고 그대로 둔다(감지=기계·해소=사용자). 장부 갱신은 **성공 시에만**
  원자적이다(base·head·recorded_at 전부).
- readonly 공유 슬롯(⑬·§F11·ADR-0061) = **detached·배타 대여 없음·session/pid 없음**. git 이 같은
  브랜치를 두 worktree 에 못 물려 detached HEAD 로 작업 슬롯과 공존한다(submodule pin 모델 동형).
  슬롯이 read-only 지 *작업*이 read-only 가 아니다 — 소비자(architect·researcher·PM domain fill)는
  활발히 쓰되 쓰기 대상이 **PM 홈 wiki** 이고 슬롯은 읽기 기준면(released base)일 뿐. 그래서 슬롯 git 을
  바꾸는 엔진 mutation(set-base/rebase/dev/sync)은 거부하고 갱신은 `refresh`(fetch→detach 이동·
  dirty=거부)만 허용한다.

## 잔여 PM 손

- 스킬은 backbone 호출만 하는 얇은 래퍼 — `dev`/`sync`/`set-base`/`status`/`rebase`/`refresh`/`record`/
  `switch` 의 stdout(무엇을 했는지·skip 사유·경고·조회 결과·rebase 요약·재기록된 branch/head·전환
  형태)을 PM 이 읽고 사용자에게 보고한다.
- `dev` 지정 후 그 submodule 에서의 실제 작업(편집·커밋)은 PM/사용자가 한다.
- 부트스트랩 0단계가 미기록 base 후보를 제시하면 사용자에게 전달·확인하고, 사용자가 고른 기준을
  `set-base` 로 기록한다(자동 채택 금지).
- rebase 는 **활성 위임 중 그 슬롯 금지**(⑳·위 선행조건) — PM 이 실행 전 그 슬롯에 활성 dev 위임이
  없는지 확인한다. rebase 충돌 시 사용자에게 그대로 두었음을 알리고 해소(continue/abort)를 위임한다.
- readonly 슬롯 생성(`/pm-env worktree add <repo> --readonly`)은 디스크=코드 전체 사본이라 결정 ⑤
  사용자 승인 flow 다 — PM 이 자율 생성하지 않는다.

## 참고

- backbone: `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`_resync_submodules_selective`/
  `set_base`/`slot_git_status`/`status`/`resolve_rebase_base`/`rebase`/`refresh`/`create_slot(readonly=)`/
  `record_git_snapshot`/`switch`).
- ADR-0049(명령어化 4요소·청중 라벨) · ADR-0051(worktree/submodule lifecycle·live-HEAD) ·
  ADR-0013(git=진실·branch 비권위) · ADR-0060(슬롯 git 진실·기대 축·기준점 미기록=사용자 질의) ·
  ADR-0061(슬롯 git 조작 + readonly 공유 슬롯·⑬·T-0358·rebase/status=T-0359).
- 라이브 하네스 테스트 = T-0278(실 LLM 시나리오 → 실 git 단언·ADR-0050).
