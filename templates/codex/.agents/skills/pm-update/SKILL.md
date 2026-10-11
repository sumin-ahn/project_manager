---
name: pm-update
description: "엔진 갱신 PM front door — pm-update.sh facade wrap + upstream freshness(경로 upstream 은 pull/경고 · URL 은 엔진이 캐시 clone/fetch) + manifest reconcile(harness-correct·PM-주도·사용자 개입 0) + adapter-drift 표면화. 채택자가 upstream 프레임워크 변경을 흡수할 때. Triggers: '엔진 갱신', 'pm-update', '프레임워크 업데이트', 'upstream 동기'."
audience: pm-internal
---

# $pm-update — 엔진 갱신 (facade-기반 PM front door)

upstream 엔진 변경을 raw `pm_update.py`가 아닌 facade(`./pm-update.sh`)로 흡수한다. URL upstream의 캐시 clone/fetch와 파일 복사는 엔진(`pm_update`)이 담당하고, 이 스킬은 로컬 경로 upstream의 pull 판단과 흡수 절차를 이끈다.

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

- **URL**: 스킬이 할 일이 없다. `./pm-update.sh`가 `<PM 홈>/.project_manager/.local/upstream-cache/<URL 해시>/`에 clone(최초)/fetch(이후)한 뒤 그 캐시를 소스로 쓰고, `upstream.rev`·`upstream.seen_rev`를 함께 기록한다. git 안전 계약(protocol allowlist·credential 미경유·redirect 차단)도 엔진이 소유한다.
  - clone/fetch 실패는 엔진이 rc≠0으로 멈춘다(옛 캐시로 진행하지 않는다) — 네트워크·URL·접근권한을 확인하고 다시 실행한다.
  - 옛 절차가 만들던 `.git-cache/upstream`은 이제 읽지 않는다. 남아 있으면 지워도 된다.

- **로컬 경로**:

  ```bash
  git -C <path> pull          # 또는 "뒤처짐" 경고만 (공동개발 worktree 면 pull 생략 가능)
  ```

  로컬 checkout rev가 seen이다.

### 3. 엔진 갱신

```bash
./pm-update.sh                      # local.conf upstream.path= 자동(URL·경로 모두)
```

manifest 경로만 byte-overwrite하며 `@render` path는 operational 토큰을 재치환한다. pm_update가 sync rev를 `upstream.rev`(baseline)와 `upstream.seen_rev`(관찰값)에 함께 기록하므로 정상 흡수 직후 두 키가 같아져 drift가 clear된다. 다른 checkout에서 받을 때만 `--from <checkout>`을 명시한다.

관리자 [적용]은 같은 명령을 새 Python 프로세스로 두 번 실행한다. v1.8.0 이상 사본은 첫 실행이 엔진 코드 전달과 완료 단계(렌더·이관·훅·어댑터·기준 rev)를 모두 끝내고, 두 번째 실행은 zero-change RUN2 확인이다. v1.7.13 이하 사본은 첫 실행을 옛 updater가 돌리므로 RUN2가 새 updater로 완료 단계를 실행하고(변경이 남을 수 있다) 세 번째 실행이 변경 0을 확인한다. 미리보기는 한 번이며 실패 뒤 자동 재실행은 없다. 구 CLI·구 facade·구 관리자 직접 진입도 같은 횟수로 실행한다. 최종 성공은 마지막 실행의 완료 결과로 판단한다. 실패는 baseline 자동 rollback 으로 설명하지 말고 실패 단계·잔존 키를 보고한다.

동기 뒤 managed config 완료 게이트를 확인한다. red면 안내된 `--accept <경로>` 또는 pm-update
재실행으로 수렴시킨 뒤 다시 검사하며, rc=0 전에는 흡수 완료로 기록하지 않는다.

```bash
./pm-config.sh sync-adapter-config --check
```
