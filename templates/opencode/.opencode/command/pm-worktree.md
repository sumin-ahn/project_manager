---
description: "worktree/submodule 운영중 관리 — submodule 을 dev 브랜치로 지정(pool selective resync 로부터 보호)·drift 난 detached submodule 을 pin 으로 수동 재동기·슬롯 기준점(base) 사용자 명시 기록(set-base)·슬롯 git 구성 조회(status)·readonly 공유 슬롯 갱신(refresh). backbone CLI .project_manager/tools/worktree_pool.py (dev/sync/set-base/status/refresh) thin wrapper. Triggers: 'submodule dev 지정', 'submodule 작업 중 선언', 'worktree submodule drift 재동기', 'worktree 브랜치 관리', '슬롯 기준점 지정', 'set-base', 'worktree status', 'readonly 슬롯 갱신', 'refresh', 'pm-worktree'."
argument-hint: "dev <submodule> <branch> --repo <repo> --slot <N>  |  sync --repo <repo> --slot <N>  |  set-base <slot> <branch>[@<commit>]  |  status [<slot>]  |  refresh <slot> [--onto <branch>]"
audience: pm-internal
---

<command-instruction>

# /pm-worktree — worktree/submodule 운영중 관리

> {{PROJECT_NAME}} 슬롯의 worktree/submodule 을 세션 중 관리하는 pm-internal 커맨드 — 어떤
> submodule 을 직접 고칠 때 **dev 브랜치로 지정**해 pool 의 selective resync(브랜치 전환·
> 부트스트랩)로부터 보호하고, drift 난 detached(consume) submodule 을 pin 으로 **수동 재동기**하며,
> 슬롯 **기준점(base)을 사용자 명시로 기록**(`set-base`)하고 슬롯 git 구성을 **조회**(`status`)한다.
> backbone = `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`set-base`/`status`). 비즈니스
> 로직 0 — 엔진 CLI 호출 thin wrapper (ADR-0049 명령어化 4요소·ADR-0051 live-HEAD 역할모델).

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 세션 중 자동 invoke. 사용자가 자연어로 "이 submodule 은 내가
작업 중"·"submodule drift 정리해" 라고 지시하면 PM 이 이 커맨드를 부른다. 셋업(pm-env·
user-entrypoint)의 확장이 아니라 **운영중-관리** — 청중이 다르다(ADR-0049).

## 사용 시점 (trigger)

- **submodule 을 직접 고치기 전** — 슬롯 worktree 안 어떤 submodule 에서 작업하려 할 때, 먼저
  그 submodule 을 dev 로 지정해 pool 이 detached pin 으로 낚아채지 않게 한다.
- **drift 재동기** — 브랜치 전환 없이 detached(consume) submodule 이 superproject pin 과 어긋났을
  때 수동 재동기한다. (상태 *확인*은 부트스트랩의 `### 슬롯 상태` 절이 이미 surface·T-0276.)
- **기준점(base) 지정** — 부트스트랩 0단계가 "기준점 미기록 — drift 감지 비활성" 을 loud 표시하고
  후보를 제시하면(v1.3.0 이전 슬롯), 사용자가 고른 기준을 `set-base` 로 기록한다("이 슬롯 base 는
  origin/main"). 그때부터 drift 감지가 작동한다(자동 추론 없음·사용자 결정·결정 ⑪).
- **슬롯 git 구성 조회** — 슬롯의 role·base·branch·head·**base 대비 N behind** 를 `status` 로 본다
  (손-git 조합 불요). 기준점 미기록이면 "base 대비 behind" 는 `-`(계산 불가).
- **readonly 공유 슬롯 갱신** — research 전용 read-only 슬롯(⑬·`worktree add --readonly` 로 생성)을
  released 최신 tip 으로 `refresh` 한다("readonly 슬롯 최신으로 갱신해"). fetch → detached HEAD 이동.
  dirty(누군가 씀·신호)면 거부(조용히 reset 안 함).

## 실행

opencode bash tool 로 실행한다. 사용자가 준 인수(`$ARGUMENTS`)에서 서브커맨드(`dev`/`sync`/
`set-base`/`status`)·submodule 경로·branch·슬롯을 추출해 아래 형태로 호출한다. 엔진이 인코딩을
코드로 처리하므로 env prefix 불필요(드물게 필요하면 셸별 문법 — bash `PYTHONUTF8=1`, PowerShell
`$env:PYTHONUTF8='1';`). 공유 루트(`.project_manager` 있는 곳)에서:

```bash
# ① submodule 을 dev 브랜치로 지정 → 이후 selective resync 가 skip (dev 작업 보호)
python3 .project_manager/tools/worktree_pool.py dev <submodule_path> <branch> --repo <repo> --slot <N>

# ② 현재 슬롯 submodule 수동 재동기 (detached=pin 재동기·on-branch=skip·dirty=skip+경고)
python3 .project_manager/tools/worktree_pool.py sync --repo <repo> --slot <N>

# ③ 슬롯 기준점(base) 사용자 명시 기록 (미기록 슬롯 해소 → 이후 drift 감지 작동·자동 추론 금지)
python3 .project_manager/tools/worktree_pool.py set-base <slot> <branch>[@<commit>]

# ④ 슬롯 git 구성 조회 (role·base·branch·head·base 대비 N behind·미기록이면 behind `-`)
python3 .project_manager/tools/worktree_pool.py status [<slot>]

# ⑤ readonly 공유 슬롯 갱신 (fetch → detached HEAD 이동 + submodule 재동기·dirty=거부+loud·⑬)
python3 .project_manager/tools/worktree_pool.py refresh <slot> [--onto <branch>]
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
  부트스트랩 0단계가 후보(예 "`origin/main`(merge-base `df10dc6`)")를
  제시한다 — 사용자가 그중 하나를 골라 이 명령으로 기록한다.
- `status`: `<slot>` 생략 시 cwd/세션 leased 슬롯으로 해소(무인자=내 슬롯). role(work/readonly)·
  base·branch·head 를 surface. "base 대비 N behind" 는 기준점이 있어야 계산된다 — 미기록이면 `-`
  (계산 불가·자동 추론 금지). rebase(기준 변경) 본체는 후속(wave-2d): 기준 없으면 거부되고
  `--onto <branch>` 로 기준을 명시해야 진행+기록된다.
- `refresh`: **readonly 공유 슬롯(⑬) 전용** — fetch → detached HEAD 를 기준(`--onto <branch>` 또는
  기록된 base.branch) 최신 tip 으로 이동하고 **submodule 을 재동기**한다(gitlink 옛 pin 잔존→stale+dirty
  자가 잠금 방지). 이동한 tip 을 base.commit 으로 재기록한다(기준면=released base 이동). **dirty 면
  거부 + loud**(read-only 슬롯의 dirty 는 "누군가 여기 썼다"는 신호 — 조용히 reset 하지 않는다·사용자가
  보고 판단). 기준 미해소(추론 금지)·대상이 readonly 가 아니면 rc 1. **lease/mutation 거부**: readonly
  슬롯에 `set-base`/`dev`/`sync`(및 rebase 본체)·`release`/바인딩(`/pm-bootstrap --slot`)은 엔진이
  거부한다(문서 검증 기준면·무소유 공유 자산·갱신은 refresh 만·⑬·§F11).
- **`--slot` 을 명시**(dev/sync)하거나 **`<slot>` 위치인자를 지정**(set-base/status)해 오타깃을
  막는다(정체성=에이전트 맥락·도구엔 명시 전달). 미해소(0)·모호(≥2)면 rc 1 로 명확히 실패한다
  (침묵 no-op 아님). submodule 경로·슬롯은 형식/목록 검증을 거쳐 슬롯 경계 밖(절대경로·`..`)은 거부된다.

## 결정 (모델 · ADR-0051 · ADR-0060)

- submodule 역할 = **live git HEAD 판별**(무스키마): on-branch=dev(보호)·detached=consume(재동기 대상).
- 전역 `submodule.recurse=true` 안 씀(dev 파괴·크럭스 A) → **selective**. dirty 가드로 미커밋 보호.
- 슬롯 기준점(base) = **rebase 로만 바뀌는 기대 축**(ADR-0060·live 표시와 별개). **미기록이면 추론
  금지·사용자 질의**(엔진=상태 surface·PM=확인·사용자=결정·결정 ⑪) — `set-base` 로 명시 지정한다.
  `merge-base` 추측은 rebase 이력·다중 후보에서 조용히 틀리고, 그 가짜 base 위에서 drift 감지가 돌면
  무의미해진다.
- readonly 공유 슬롯(⑬·§F11) = **detached·배타 대여 없음·session/pid 없음**. git 이 같은 브랜치를 두
  worktree 에 못 물려 detached HEAD 로 작업 슬롯과 공존한다(submodule pin 모델 동형). 슬롯이 read-only
  지 *작업*이 read-only 가 아니다 — 소비자(architect·researcher·PM domain fill)는 활발히 쓰되 쓰기
  대상이 **PM 홈 wiki** 이고 슬롯은 읽기 기준면(released base)일 뿐. 그래서 슬롯 git 을 바꾸는 엔진
  mutation(set-base/rebase/dev/sync)은 거부하고 갱신은 `refresh`(fetch→detach 이동·dirty=거부)만.

## 잔여 PM 손

- 커맨드는 backbone 호출만 하는 얇은 래퍼 — `dev`/`sync`/`set-base`/`status` 의 stdout(무엇을
  했는지·skip 사유·경고·조회 결과)을 읽고 사용자에게 보고한다.
- `dev` 지정 후 그 submodule 에서의 실제 작업(편집·커밋)은 사용자가 한다.
- 부트스트랩 0단계가 미기록 base 후보를 제시하면 사용자에게 전달·확인하고, 사용자가 고른 기준을
  `set-base` 로 기록한다(자동 채택 금지).

## 참고

- backbone: `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`_resync_submodules_selective`/
  `set_base`/`slot_git_status`/`resolve_rebase_base`/`refresh`/`create_slot(readonly=)`).
- ADR-0049(명령어化 4요소·청중) · ADR-0051(worktree/submodule lifecycle·live-HEAD) ·
  ADR-0013(git=진실·branch 비권위) · ADR-0060(슬롯 git 진실·기대 축·기준점 미기록=사용자 질의) ·
  ADR-0061(슬롯 git 조작 + readonly 공유 슬롯·⑬·T-0358). 라이브 하네스 테스트 = T-0278(ADR-0050).

</command-instruction>
