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
./pm-config.sh worktree add <repo>              # 작업 슬롯(배타 대여·세션 바인딩)
./pm-config.sh worktree add <repo> --readonly   # readonly 공유 슬롯(⑬·research 기준면)
```
추가 후 PM 에게 안내: **"이제 `/pm-bootstrap <repo> --slot N` 으로 이 슬롯에 바인딩하세요"**
— `pm_bootstrap` 의 multi-PM identity surface(T-0074)와 연결(정체성=세션 맥락).
- **`--readonly`**(⑬·T-0358): research 전용 **read-only 공유 슬롯** — 코드를 *읽어* PM 홈 wiki
  (domain·architecture·status)를 쓰는 읽기 기준면이다. detached HEAD(released base·git 이 같은 브랜치
  두 worktree 를 못 물림)·role=readonly·**session/pid 없음·배타 대여 없음**(공유가 정상). 무소유 공유
  자산이라 **바인딩(`/pm-bootstrap --slot`)·release 도 거부**되고, 갱신은 `/pm-worktree refresh` 로만
  (set-base/rebase/dev/sync 도 거부). 제거는 `worktree remove --force`.

### slot status / release / remove
```bash
./pm-config.sh status | whoami        # 2축 cockpit — task 상황 + slot 풀·슬롯당 git 요약
./pm-config.sh release <slot> [--force]        # 작업완료 반납(idle 화·재사용) / --force=강제 백스톱
./pm-config.sh worktree remove <slot> [--force] # 슬롯 통째 제거(원자·번호 재사용·T-0333)
```
- **status 2축 cockpit**(⑥·§F8·T-0361): 다슬롯 관리 부담을 두 축으로 한눈에 —
  ① **task 상황** = 사람이 명명한 task 별 {보유 작업공간(`work/<repo>_<N>`)·prefix}(slot-모드 세션 제외).
  ② **slot 풀** = 슬롯별 {state·보유 task·role(work/readonly)} + **슬롯당 git 요약**
  `<branch>@<head> (base: <b>@<sha> · N behind)`. behind 는 base 기록(`set-base`·T-0350)이 있을 때만
  세고, 미기록이면 `-` + 이유(자동 추론 금지·결정 ⑪). readonly 슬롯은 branch `(detached)`·base 만 의미.
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

### alloc / release --task / task end — task 단위 자원 대여 (F2·F3·F4·⑤·T-0354)

task = 슬롯과 **직교**하는 작업스트림 정체성(⑥). task 명의로 슬롯을 대여/반납하고 task 를 종료한다:
```bash
./pm-config.sh alloc <repo> --task <이름>          # idle 최소 번호 슬롯을 task 명의로 대여
./pm-config.sh release <slot> --task <이름>        # task 소유검사 후 반납(내 task 슬롯만)
./pm-config.sh task end <이름>                      # task 종료 — 소진 게이트 + 일괄 반납 + 아카이브
```
- **alloc**(PM 자율·논리층·⑤): idle **최소 번호** 슬롯을 `--task` 명의(lease session)로 leased 전이.
  풀에 idle 슬롯이 없으면 **자동 생성하지 않고**(디스크=코드 전체 사본×슬롯) `worktree add <repo>`
  **승인 요청**으로 멈춘다 — create/remove(물리층)=사용자 승인, alloc/release(논리층)=PM 자율(2층 분리·⑤).
- **release --task**: 그 슬롯이 내 task 명의(session)가 아니면 거부(다른 task 슬롯 보호). dirty 거부는
  현행 유지(`--force`=stash 보존 강제·소유검사 우회 백스톱). clean=idle 반납(폴더 유지·풀 재사용).
- **task end**: ① 이 task 명의로 **claimed 인 티켓**이 남아있으면 목록 + 거부(소진 게이트·⑲) — 해소는
  `board complete`(완료) 또는 `board unclaim`(claimed→open)로 **사용자 판단**(task end 가 자동 실행 안 함).
  ② 보유 작업공간 **dirty** 면 목록 + 거부. ③ 전부 clean 이면 보유 슬롯 일괄 idle 반납(worktree **삭제
  안 함**) + 장부 task 레코드 제거 + 서술 폴더를 `.local/tasks/_ended/<이름>-<날짜>/` 로 **이동**(삭제
  아님·이름 재사용 시 옛 pm_state 오염 방지·②). task 지정 prefix 의 open 티켓은 **정보 표시만**(차단 안 함·①).

### task prefix — task 의 ticket prefix 지정/변경/해제 (F5·중간 변경 자유·T-0357)

task 의 ticket prefix 를 opt-in 으로 지정/변경/해제한다 — prefix 는 task 와 **완전 독립·분류 라벨이지
경계 아님**(claim 강제 없음·①):
```bash
./pm-config.sh task prefix <이름> <p>       # task <이름> 의 board prefix 를 <p> 로 지정/변경
./pm-config.sh task prefix <이름> none      # 해제(무prefix·T-NNNN 로 발행)
```
- **중간 변경 자유** — task 진행 중 언제든 지정/변경/해제(task 종속으로 못 바꾸는 설계 금지·①ⓒ). 기본은
  **없음**(opt-in).
- **포맷**: ADR-0042 `[a-z0-9_]` 형식(그 외 rc1·소문자 권장). `none` 은 해제 리터럴(무prefix). task
  미존재면 rc1(생성은 `/pm-bootstrap --task` 단일 지점).
- **`board.py new` 3단 해소**: 명시 `--prefix` > task 지정 prefix > 기본 없음. task 에 prefix 를 지정하면
  그 task 명의(`--task <이름>`) 발행이 자동으로 그 prefix 를 단다(명시 `--prefix` 가 이김·1회 오버라이드).
- prefix 는 **분류 라벨이지 경계가 아니다** — claim 에 prefix 강제 없음. 티켓 카테고리 *관리*(list/rename/
  merge)는 별개 표면 `board.py prefix`(현행 불변).

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
