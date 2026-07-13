---
description: "worktree/submodule 운영중 관리 — submodule 을 dev 브랜치로 지정(pool selective resync 로부터 보호)·drift 난 detached submodule 을 pin 으로 수동 재동기. backbone CLI .project_manager/tools/worktree_pool.py (dev/sync) thin wrapper. Triggers: 'submodule dev 지정', 'submodule 작업 중 선언', 'worktree submodule drift 재동기', 'worktree 브랜치 관리', 'pm-worktree'."
argument-hint: "dev <submodule> <branch> --slot work/<repo>_<N>  |  sync --slot work/<repo>_<N>"
audience: pm-internal
---

<command-instruction>

# /pm-worktree — worktree/submodule 운영중 관리

> {{PROJECT_NAME}} 슬롯의 worktree/submodule 을 세션 중 관리하는 pm-internal 커맨드 — 어떤
> submodule 을 직접 고칠 때 **dev 브랜치로 지정**해 pool 의 selective resync(브랜치 전환·
> 부트스트랩)로부터 보호하고, drift 난 detached(consume) submodule 을 pin 으로 **수동 재동기**한다.
> backbone = `.project_manager/tools/worktree_pool.py`(`dev`/`sync`). 비즈니스 로직 0 — 엔진 CLI
> 호출 thin wrapper (ADR-0049 명령어化 4요소·ADR-0051 live-HEAD 역할모델).

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

## 실행

opencode bash tool 로 실행한다. 사용자가 준 인수(`$ARGUMENTS`)에서 서브커맨드(`dev`/`sync`)·
submodule 경로·branch·슬롯을 추출해 아래 형태로 호출한다. 엔진이 인코딩을 코드로 처리하므로
env prefix 불필요(드물게 필요하면 셸별 문법 — bash `PYTHONUTF8=1`, PowerShell `$env:PYTHONUTF8='1';`).
공유 루트(`.project_manager` 있는 곳)에서:

```bash
# ① submodule 을 dev 브랜치로 지정 → 이후 selective resync 가 skip (dev 작업 보호)
python3 .project_manager/tools/worktree_pool.py dev <submodule_path> <branch> --slot work/<repo>_<N>

# ② 현재 슬롯 submodule 수동 재동기 (detached=pin 재동기·on-branch=skip·dirty=skip+경고)
python3 .project_manager/tools/worktree_pool.py sync --slot work/<repo>_<N>
```

- `<submodule_path>` = 슬롯 worktree 상대 경로(= `git submodule status` 표기·예 `vendor/sub`).
- `dev`: 그 submodule 을 on-branch(dev 역할)로 만든다 — 이후 브랜치 전환·부트스트랩의 selective
  resync 가 그 submodule 을 **skip**(detached pin 으로 안 낚아챈다·ADR-0051 크럭스 A 보호).
- `sync`: pool selective 재동기(T-0275)를 수동 트리거한다. **dev(on-branch)·dirty submodule 은
  자동 보호**(skip) — 되돌릴 수 없는 pin 재동기는 detached & clean submodule 에만 일어난다.
- **`--slot` 을 명시**해 오타깃을 막는다(정체성=에이전트 맥락·도구엔 명시 전달). 미해소(0)·
  모호(≥2)면 rc 1 로 명확히 실패한다(침묵 no-op 아님). submodule 경로·슬롯은 형식/목록 검증을
  거쳐 슬롯 경계 밖(절대경로·`..`)은 거부된다.

## 결정 (모델 · ADR-0051)

- submodule 역할 = **live git HEAD 판별**(무스키마): on-branch=dev(보호)·detached=consume(재동기 대상).
- 전역 `submodule.recurse=true` 안 씀(dev 파괴·크럭스 A) → **selective**. dirty 가드로 미커밋 보호.

## 잔여 PM 손

- 커맨드는 backbone 호출만 하는 얇은 래퍼 — `dev`/`sync` 의 stdout(무엇을 했는지·skip 사유·경고)을
  읽고 사용자에게 보고한다.
- `dev` 지정 후 그 submodule 에서의 실제 작업(편집·커밋)은 사용자가 한다.

## 참고

- backbone: `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`_resync_submodules_selective`).
- ADR-0049(명령어化 4요소·청중) · ADR-0051(worktree/submodule lifecycle·live-HEAD) ·
  ADR-0013(git=진실·branch 비권위). 라이브 하네스 테스트 = T-0278(ADR-0050).

</command-instruction>
