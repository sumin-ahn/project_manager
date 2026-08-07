---
name: pm-update
description: "엔진 갱신 PM front door — pm-update.sh facade wrap + upstream freshness 자동분기(URL→cache clone/fetch·경로→pull/경고) + manifest reconcile(harness-correct·PM-주도·사용자 개입 0) + adapter-drift 표면화. 채택자가 upstream 프레임워크 변경을 흡수할 때. Triggers: '엔진 갱신', 'pm-update', '프레임워크 업데이트', 'upstream 동기'."
audience: pm-internal
---

# /pm-update — 엔진 갱신 (facade-기반 PM front door)

upstream 엔진 변경을 raw `pm_update.py`가 아닌 facade(`./pm-update.sh`)로 흡수한다. git freshness는 이 스킬층, 파일 복사는 엔진(`pm_update`)이 담당한다.

> **Windows 진입**: `./pm-update.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-update.cmd`**(동일 인자·아래 `./pm-config.sh` 참조도 동형 **`.\pm-config.cmd`**).
> 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신 **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## 범위

- **pm-update** = 엔진 + host 어댑터 갱신(upstream→채택자).
- upstream 값 전환은 [[pm-env]], 세션 시작 상태점검은 [[pm-bootstrap]].
- **dual-harness guest**(`add-harness`로 얹은 `.opencode/*` 등)는 engine.manifest guest 절 안에서 두 채널로 갈린다 — 한 줄의 `@render` 유무가 소유를 정한다.
  - **어댑터 렌더물**(`@render @target-owned`, 예 `.opencode/agents`): pm-update는 갱신하지 않는다. `add-harness <harness>`를 재실행한다(refresh·기존 인스턴스 위 live-safe).
  - **어댑터 엔진 파일**(`@source=templates/<flavor>/… @target-owned`, 예 `.opencode/lib`·`.codex/pm_orch_codex.py`·claude ctx 가드): pm-update가 byte-copy로 갱신한다. `pm_relay` 코어와 짝인 파일들이라 이 채널이 없으면 코어↔드라이버 skew가 쌓인다.
- 구버전에서 얹은 guest 절에는 엔진 행이 없을 수 있다. 이 경우 pm-update가 flavor를 1회 추론해 엔진 행을 절에 기록하며(이후 실행은 기록된 `@source` provenance를 따른다), 화면에 `guest 엔진 행 N건 파생`으로 표시한다.

사용 시점: upstream 변경/주기적 freshness 점검 또는 `board.py lint`의 `adapter-drift` advisory 발생 시.

## 실행 절차

### 1. upstream 값 확인

```bash
./pm-config.sh upstream show
```

URL(`https://`·`ssh://`·`file://`) 또는 로컬 경로 모양으로 분기한다.

### 2. upstream freshness

- **URL**: 엔진은 URL에서 직접 복사하지 못하므로 cache clone/fetch 후 `--from`에 로컬 checkout을 준다.

  ```bash
  # 안전 git env — 엔진 pm_import.py 의 _UPSTREAM_GIT_CONFIG_KV(6키)와 동일해야 한다(엔진 변경 시 동기).
  # redirect off + protocol allowlist(https/ssh/file 만·file 포함=valid URL form) + credential 미경유.
  GIT="git -c protocol.allow=never -c protocol.https.allow=always -c protocol.ssh.allow=always -c protocol.file.allow=always -c http.followRedirects=false -c credential.helper="
  $GIT clone <url> <cache>          # 최초 (cache 위치=채택자 소유·예 .git-cache/upstream)
  $GIT -C <cache> fetch --all       # 이후
  SEEN=$($GIT -C <cache> rev-parse HEAD)
  ```

  fetch 후 `$SEEN`을 채택자 `local.conf`의 **`upstream_seen_rev=<rev>`**로 기록한다. set-or-replace(그 줄만 교체, 없으면 append, 기존 키·주석 보존; `pm_config upstream set` 백엔드와 동형)한다. 이는 drift-lint 입력이며 baseline `upstream_rev`와 **별개 키**다. 이후 `--from <cache>`.
  URL clone/fetch의 redirect·host allowlist·submodule guard는 위 `$GIT` env로 이 스킬이 강제한다.

- **로컬 경로**:

  ```bash
  git -C <path> pull          # 또는 "뒤처짐" 경고만 (공동개발 worktree 면 pull 생략 가능)
  ```

  로컬 checkout rev가 seen이다. pm_update가 동기 시 `upstream_rev`(baseline)와 `upstream_seen_rev`(관찰값)를 함께 기록하므로 정상 흡수 직후 두 키가 같다.

### 2.5 변경점 미리보기 (sync 전)

baseline(local.conf `upstream_rev`)과 cache/경로 HEAD 사이 commit 수 및 받을 엔진파일을 read-only(`git log`/`diff`, fetch 0)로 본다.

```bash
./pm-update.sh --changes --from <cache-or-path>   # commit 수 + 4버킷 분리
```

출력 4버킷 (분류 기준은 실 sync의 계획 manifest와 같다 — guest 절 채널 분리 포함):

| 버킷 | 뜻 |
|---|---|
| 헤더 | `baseline → HEAD (N commits)` |
| 엔진 영향 (manifest 경로·이번 동기가 받는 것) | 이번 sync가 덮어쓸 파일 |
| 그 외 변경 (manifest 밖·동기 안 받음) | upstream은 바뀌었으나 이 인스턴스가 등재하지 않은 경로 |
| 상류 삭제·rename (동기가 지우지 않음) | upstream에서 사라진 등재 경로. 이어지는 `상류 부재 파일` 보고가 dest에 잔존하는 실물을 나열한다(동기는 지우지 않음·수동 정리 판단) |

- **변경 0(최신)**: 동기 생략.
- **변경 > 0**: "엔진 영향(이번 동기가 받는 것)" 목록을 PM에게 보고한 뒤 reconcile → sync.
- baseline 미기록(첫 동기·구 import): "다음 sync 후 추적" 안내가 정상이며 그대로 진행.

### 3. manifest reconcile (pm_update 전·PM 주도·사용자 개입 0)

**기본 경로 = 선-cp 없이 4단계 sync 만 실행한다.** `@source` self-prop 채택자(현행 import 전원)는 엔진이 manifest 신 항목 도달을 스스로 처리한다 — 등재 행이 지워진 채택자도 sync 1회가 행 복구 + 파일 전파 + guest 절 보존을 함께 수행한다(실측 게이트 박제).

수동 cp 는 self-prop 이 없는 구세대 manifest 형상에서만 쓴다:

```bash
cp <cache-or-path>/templates/<harness>/.project_manager/engine.manifest .project_manager/engine.manifest
```

- ⚠️ **루트 manifest가 아니라 `templates/<harness>/` manifest**(`<harness>`=이 채택자의 claude_code | opencode)를 쓴다. 루트는 claude-scoped라 opencode 채택자를 clobber한다.
- ⚠️ **cp 는 파일 덮어쓰기라 add-harness guest 절이 통째로 사라진다**(보존 로직이 읽을 절이 dest 에 없어짐). cp 를 썼고 guest 하네스가 있다면 **직후 `./pm-config.sh add-harness <guest>` 재실행**으로 절을 되살린다 — sync 출력의 `미등재 flavor 파일 관측` 경고가 같은 복구를 안내한다.

### 4. 엔진 갱신

```bash
./pm-update.sh --from <cache-or-path>     # --from 생략 시 local.conf upstream= 자동(경로일 때만)
```

manifest 경로만 byte-overwrite하며 `@render` path는 operational 토큰을 재치환한다. pm_update가 sync rev를 `upstream_rev`로 기록한다. 경로 upstream이면 `upstream_seen_rev`도 같은 rev로 기록하고, URL이면 2단계에서 기록한 seen과 같아져 drift가 clear된다.

⚠️ URL upstream에서 `--from`을 생략하면 엔진이 에러로 멈춘다. cache 경로를 `--from`으로 준다.

### 5. drift 표면화

```bash
python3 .project_manager/tools/board.py lint
```

`adapter-drift` advisory가 남으면 PM에게 보고하고 수기 검토를 안내한다(never-block). 이는 baseline↔관찰 rev 불일치로 manifest 제외 facade·진입문서 등이 낡았을 수 있음을 뜻한다. lint는 git을 하지 않아 어느 rev가 앞섰는지 판정하지 않는다. 자동전파는 customization을 clobber할 수 있어 금지하며, 실제 선후·변경분은 `--changes`로 본다.
