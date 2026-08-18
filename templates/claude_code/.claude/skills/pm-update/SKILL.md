---
name: pm-update
description: "엔진 갱신 PM front door — pm-update.sh facade wrap + upstream freshness 자동분기(URL→cache clone/fetch·경로→pull/경고) + manifest reconcile(harness-correct·PM-주도·사용자 개입 0) + adapter-drift 표면화. 채택자가 upstream 프레임워크 변경을 흡수할 때. Triggers: '엔진 갱신', 'pm-update', '프레임워크 업데이트', 'upstream 동기'."
audience: pm-internal
---

# /pm-update — 엔진 갱신 (facade-기반 PM front door)

upstream 엔진 변경을 raw `pm_update.py`가 아닌 facade(`./pm-update.sh`)로 흡수한다. git freshness는 이 스킬층, 파일 복사는 엔진(`pm_update`)이 담당한다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 범위

- **pm-update** = 엔진 + host 어댑터 갱신(upstream→채택자).
- upstream 값 전환은 [[pm-env]], 세션 시작 상태점검은 [[pm-bootstrap]].
- **dual-harness guest**(`add-harness`로 얹은 `.opencode/*` 등)도 engine.manifest guest 절째로 pm-update가 전파한다 — 한 줄의 `@render` 유무가 **전파 방식**을 정한다(채널은 하나다).
  - **어댑터 렌더물**(`@render @target-owned`, 예 `.opencode/agents`·`.claude/agents`): pm-update가 채택자 `local.conf` 값으로 **다시 렌더**한다. 카드의 `model`을 손으로 고쳐도 다음 pm-update가 conf 값으로 되돌린다 — 값을 바꾸는 자리는 `local.conf`의 `delegate.<role>[.<tier>].{model,reasoning}`다. `add-harness <harness>` 재실행은 그 하네스 어댑터 파일이 새로 추가/폐기됐을 때 쓰고, 값 반영에는 필요하지 않다.
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

### 4. 엔진 갱신

```bash
./pm-update.sh --from <cache-or-path>     # --from 생략 시 local.conf upstream= 자동(경로일 때만)
```

manifest 경로만 byte-overwrite하며 `@render` path는 operational 토큰을 재치환한다. pm_update가 sync rev를 `upstream_rev`로 기록한다. 경로 upstream이면 `upstream_seen_rev`도 같은 rev로 기록하고, URL이면 2단계에서 기록한 seen과 같아져 drift가 clear된다.

⚠️ URL upstream에서 `--from`을 생략하면 엔진이 에러로 멈춘다. cache 경로를 `--from`으로 준다.

동기 뒤 managed config 완료 게이트를 확인한다. red면 안내된 `--accept <경로>` 또는 pm-update
재실행으로 수렴시킨 뒤 다시 검사하며, rc=0 전에는 흡수 완료로 기록하지 않는다.

```bash
./pm-config.sh sync-adapter-config --check --from <cache-or-path>
```
