---
name: pm-update
description: "엔진 갱신 PM front door — pm-update.sh facade wrap + upstream freshness 자동분기(URL→cache clone/fetch·경로→pull/경고) + manifest reconcile(harness-correct·PM-주도·사용자 개입 0) + adapter-drift 표면화. 채택자가 upstream 프레임워크 변경을 흡수할 때. Triggers: '엔진 갱신', 'pm-update', '프레임워크 업데이트', 'upstream 동기'."
---

# /pm-update — 엔진 갱신 (facade-기반 PM front door)

> 채택자가 upstream 프레임워크 엔진 변경을 흡수한다. raw `pm_update.py` 대신 이 스킬을 invoke —
> facade(`./pm-update.sh`) backbone 위에 **upstream freshness 자동분기 · manifest reconcile · drift 표면화**를
> 얹는다. 엔진(`pm_update`)은 파일-복사만(git 무지·ADR-0032 D5) — git freshness 는 이 스킬층이 담당.

## 인접 스킬과 구분
- **pm-update (이 스킬)** = 엔진 *갱신*(upstream→채택자).
- [[pm-env]] = 환경 관리(repo/worktree/slot · upstream show/switch). upstream *값* 전환은 거기서.
- [[pm-bootstrap]] = 세션 시작 상태점검(갱신 아님).

## 사용 시점
- upstream(프레임워크)에 새 엔진 변경이 났을 때 · 주기적 freshness 점검.
- `board.py lint` 가 `adapter-drift` advisory 를 냈을 때(= upstream 이 baseline 이후 앞섬).

## 실행 절차

### 1. upstream 값 확인
```bash
./pm-config.sh upstream show
```
URL(`https://`·`ssh://`·`file://`) 또는 로컬 경로. 값 *모양*으로 아래 분기(self-describing).

### 2. upstream freshness (값 모양 자동분기)
- **URL** (릴리스 추적·신규 채택자 기본) — 엔진은 URL 에서 직접 복사 못 한다(파일-복사). cache clone/fetch 로 로컬 체크아웃을 만들고 그걸 `--from` 으로 준다:
  ```bash
  # 안전 git env — 엔진 pm_import.py 의 _UPSTREAM_GIT_CONFIG_KV(6키)와 동일해야 한다(엔진 변경 시 동기).
  # redirect off + protocol allowlist(https/ssh/file 만·file 포함=valid URL form) + credential 미경유 — ADR-0032 D5.
  GIT="git -c protocol.allow=never -c protocol.https.allow=always -c protocol.ssh.allow=always -c protocol.file.allow=always -c http.followRedirects=false -c credential.helper="
  $GIT clone <url> <cache>          # 최초 (cache 위치=채택자 소유·예 .git-cache/upstream)
  $GIT -C <cache> fetch --all       # 이후
  SEEN=$($GIT -C <cache> rev-parse HEAD)
  ```
  fetch 후 cache HEAD(`$SEEN`)를 채택자 `local.conf` 에 **`upstream_seen_rev=<rev>`** 로 기록 — set-or-replace 규율(그 줄만 교체·없으면 append·기존 키·주석 보존, `pm_config upstream set` 백엔드와 동형). drift-lint(T-0141) 입력이고 baseline `upstream_rev` 와 **별개 키**(한 키 2역 금지). 이후 `--from <cache>`.
- **로컬 경로** (엔진 공동개발·도그푸딩):
  ```bash
  git -C <path> pull          # 또는 "뒤처짐" 경고만 (공동개발 worktree 면 pull 생략 가능)
  ```
  경로면 로컬 checkout rev 가 곧 seen — pm_update 가 동기 시 직접 읽어 baseline 기록.

### 2.5 변경점 미리보기 (sync 전 · 무엇이 올지)
sync 로 받을 변경을 미리 본다 — baseline(local.conf `upstream_rev`) ↔ cache/경로 HEAD 의
commit 수 + 받을 엔진파일. 엔진 read-only(T-0146·`git log`/`diff`·fetch 0·ADR-0032 D5).
```bash
./pm-update.sh --changes --from <cache-or-path>   # commit 수 + 엔진 영향(받는 것)/그 외 분리
```
- **변경 0(최신)** — 받을 게 없으니 §3·§4 생략(이번 동기 불요).
- **변경 > 0** — "엔진 영향(이번 동기가 받는 것)" 목록을 PM 에게 보고한 뒤 §3 reconcile → §4 sync 진행.
- baseline 미기록(첫 동기·구 import)이면 "다음 sync 후 추적" 안내가 정상 — 그대로 §3·§4 진행.

### 3. manifest reconcile (pm_update *전* · PM-주도 · 사용자 개입 0)
upstream 의 **harness-correct** manifest 를 채택자로 먼저 맞춘다 — 새 엔진 항목(예 pm_import.py)이 *기존* 채택자에 도달하려면 채택자 manifest 가 그 항목을 먼저 알아야 한다(pm_update 는 dest manifest 우선).
```bash
cp <cache-or-path>/templates/<harness>/.project_manager/engine.manifest .project_manager/engine.manifest
```
⚠️ **루트 manifest 가 아니라 `templates/<harness>/` manifest**(`<harness>`=이 채택자의 claude_code | opencode) — 루트는 claude-scoped 라 opencode 채택자에 clobber(codex round-2·self-list 폐기).

### 4. 엔진 갱신 (facade)
```bash
./pm-update.sh --from <cache-or-path>     # --from 생략 시 local.conf upstream= 자동(경로일 때만)
```
manifest 경로만 byte-overwrite(@render path 는 operational 토큰 재치환). `pm_update` 가 이 sync 의 upstream rev 를 `upstream_rev`(baseline) 로 기록 → 이번 동기 후 baseline==seen(drift clear).
> upstream 이 URL 인데 `--from` 을 생략하면 엔진이 명확한 에러로 멈춘다(엔진은 URL 복사 못 함) — 위 cache 경로를 `--from` 으로 준다.

### 5. drift 표면화
```bash
{{PY}} .project_manager/tools/board.py lint
```
`adapter-drift` advisory 가 남아 있으면(facade·진입문서 등 manifest-제외 잔여가 upstream 이후 변경) PM 에게 보고 — 자동전파 대상 아님(B 전파=채택자 customization clobber·비파괴), 수기 검토 안내(never-block).

## 결정
- 엔진(`pm_update`) 무변경 — git freshness 는 이 스킬층(이식성·오프라인·도그푸딩 보존·ADR-0032 D5).
- facade(`./pm-update.sh`) backbone — self-locating·cwd-robust. 기존 `pm-*` 스킬 thin-wrapper 패턴 동형.
- manifest reconcile = harness-correct(self-list 아님)·PM-주도(사용자 개입 0·codex round-2).
- URL clone/fetch 의 redirect/host-allowlist/submodule 가드는 이 스킬이 강제(위 `$GIT` env) — 엔진 도달성 호출 밖 표면(ADR-0032 D5 분담).

## 참고
- 설계: ADR-0032(D3 스킬화·D4 upstream 하이브리드·D5 엔진/스킬 경계) · backbone facade `pm-update.sh`→`pm_update.py`.
- [[pm-env]](upstream 전환) · drift-lint = `board.py lint` adapter-drift(T-0141).
