---
description: "PM 환경 관리 단일 command — pm-config.sh facade wrap. repo add · worktree add(→pm-bootstrap 바인딩 안내) · slot status/release/remove · upstream show/switch(path↔URL). multi-PM 셋업·upstream 전환의 단일 진입. Triggers: 'pm-env', 'repo 추가', 'worktree 추가', 'slot 상태', '슬롯 제거', 'upstream 전환', '환경 관리'."
---

<command-instruction>

# /pm-env — PM 환경 관리 (pm-config facade)

> {{PROJECT_NAME}} PM 환경 셋업·조회를 한 command 로 — `./pm-config.sh` facade backbone 위에
> repo/worktree/slot/upstream 분기. multi-PM 토폴로지(여러 repo·worktree 슬롯)와 upstream 값 전환의 단일
> 진입. thin — `pm-config` 가 CLI 계약 단일 진실(서브커맨드 추가돼도 이 command 변경 불필요).

> **Windows 진입**: `./pm-config.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-config.cmd`**(동일 인자·
> pm_import 가 루트로 복사). PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신
> **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## 인접 command 와 구분
- **pm-env (이 command)** = 환경 *셋업/조회*(repo·worktree·slot·upstream 값).
- `/pm-update` = 환경이 가리키는 upstream 으로 엔진 *갱신*. upstream 값을 여기서 전환 → pm-update 가 적용.
- `/pm-bootstrap` = 세션 *시작* 시 슬롯 바인딩·상태점검.

## 분기 (trigger·인자로)

### repo add — multi-PM repo 등록
```bash
./pm-config.sh repo add <name> --git <url> --test "<cmd>"
```
`areas.md` 공유 레지스트리 등록 + per-repo 셋업. 이후 worktree 슬롯을 붙인다.

### worktree add — 슬롯 생성 (→ bootstrap 바인딩 안내)
```bash
./pm-config.sh worktree add <repo>
```
추가 후 PM 에게 안내: **"이제 `/pm-bootstrap <repo> --slot N` 으로 이 슬롯에 바인딩하세요"**
— `pm_bootstrap` 의 multi-PM identity surface(T-0074)와 연결(정체성=세션 맥락).

### slot status / release / remove
```bash
./pm-config.sh status | whoami        # 풀/리스 + 이 세션 repo/슬롯/branch
./pm-config.sh release <slot> [--force]        # 작업완료 반납(idle 화·재사용) / --force=강제 백스톱
./pm-config.sh worktree remove <slot> [--force] # 슬롯 통째 제거(원자·번호 재사용·T-0333)
```
- **release vs remove**: `release` = idle 화(슬롯 폴더 유지·풀 재사용). `remove` = **통째 제거** —
  `git worktree remove` + 슬롯 전용 브랜치(`<repo>_<N>`) 정리(머지 완료 시 삭제·미머지 보존·공유 브랜치 스킵)
  + 장부 엔트리 삭제. 장부까지 지워 `add` 가 **빈 번호를 재사용**한다(수동 remove → dangling 장부 →
  번호 skip footgun 종결). dirty/활성 리스는 거부(`--force` 로만·dirty 는 stash 보존).
- **제거 3분법**: 등록 슬롯 통째=`worktree remove <slot>` / dangling 장부(worktree 부재)=`worktree
  prune-stale`(안전) / orphan worktree(장부 미등록)=사용자 `git worktree remove`. **사용자 명시 호출
  전제** — PM 이 자율로 슬롯을 제거하지 않는다(삭제-위임 원칙).
- **캐비앗**: 미머지 전용 브랜치는 보존되며(작업 유실 방지) 같은 번호 슬롯 재생성은 브랜치 잔존을
  선-검출해 명확한 진단(`SlotBranchExists`·T-0335)으로 멈춘다 — 보존 브랜치 정리(머지/삭제) 후
  재시도하거나, 그 작업을 재개하려면 **수동 checkout**(`git worktree add <슬롯경로> <브랜치>`·리셋
  없음)으로 (기존 브랜치는 엔진 branch-경로도 T-0343 부터 리셋 없는 checkout — 유실 클래스 종결).
  `--force` 로 활성(사용 중) 슬롯을 강제 회수하면 '⚠ 강제 회수' 경고가 stderr 로 뜨고, dirty 를
  stash 보존하면 '복구: git stash list/pop (공유 refs/stash)' 안내가 나온다(리뷰 반영·T-0333).

### upstream show / switch (path ↔ URL · T-0145)
```bash
./pm-config.sh upstream show
./pm-config.sh upstream set <url|path>
```
`set` 은 검증 후 `local.conf upstream=` atomic 재기록(타 키 보존·fail-closed): URL→`git ls-remote` 도달성 · 경로→존재+checkout. 값 self-describing(https/ssh/file→URL · 그 외→경로)이라 **전환 후 `/pm-update` 가 자동 적응**(URL→cache clone · 경로→pull).

### worktree add timeout 노브 — 하네스 false-kill 방지 (T-0293·3-layer)

대형 repo `worktree add`(로컬 bare→full checkout·느린 디스크/VPN/Windows)가 *진행 중인데도* 짧은
고정 타임아웃에 죽는(false-kill) 걸 막는 3-layer 노브. 기본 **30분**·튜닝 가능:

- **엔진** `PM_GIT_TIMEOUT`(초·`none`/`0`/`unlimited`=무제한·기본 1800) — worktree add console-visible
  러너(T-0292). `export PM_GIT_TIMEOUT=none` 으로 초대형 repo 무제한(진행 콘솔 가시·hang 은 Ctrl-C).
- **opencode 하네스** `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`(ms) — opencode bash 툴 기본
  120초. **opencode 는 config 파일로 못 실어**(`.env` 자동로드 없음·실측) → **shell export** 로:
  `export OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=1800000`(shell 프로파일·`.envrc`/direnv·
  opencode 실행 쉘에 상속). `EXPERIMENTAL` = 버전 의존이니 **회사 버전서 라이브 확인**(라이브 실측 규율).
- **claude 하네스**(참고·claude 어댑터) `BASH_DEFAULT/MAX_TIMEOUT_MS`(ms·`.claude/settings.json` env).

넘으면 엔진 트립 메시지가 "터미널 직접 실행(`PM_GIT_TIMEOUT=none`)"을 안내한다. opencode 로 worktree add 가
느린 대형 repo 에서 죽으면 위 export 를 먼저 확인.

### opencode stall 워치독 노브 — 무한 hang 방지 (T-0336)

opencode `run` 스타트업 fetch stall(간헐 brownout·자체 회복 없음·실측 PM 70)을 호출층 첫-이벤트
워치독이 kill+재시도로 닫는다. 노브(env·초):
- `PM_OC_FIRST_EVENT_TIMEOUT`(기본 90) — 첫 json 이벤트까지 무소식 허용 시간.
- `PM_OC_STALL_RETRIES`(기본 2) — 소진 시 fail-loud. 각 재시도는 stderr 1줄 loud.
적용 표면: relay driver·pm_import `--fill auto`·release 라이브 테스트 (provider/원인 무관).


## 결정
- **단일 command**(trigger/인자 분기·사용자 확정) — `pm-config` 대화형 콘솔과 동형 진입.
- thin — 비즈니스 로직 0. upstream 전환 백엔드(검증·atomic·디커플)는 엔진(`pm_config upstream`·T-0145).
- ⚠️ opencode 채택자: 이 command(`.opencode/command/`)는 `@target-owned` 라 `pm_update` 가 전파하지 않는다 — 새 command 는 **re-import 로 도달**. `/pm-import` 가이드의 re-import 경로 참조.

## 참고
- 설계: ADR-0032(D3 스킬화·D4 upstream 하이브리드) · backbone facade `pm-config.sh`→`pm_config.py`.
- `/pm-update`(전환한 upstream 으로 갱신) · `/pm-bootstrap`(worktree add 후 슬롯 바인딩).

</command-instruction>
