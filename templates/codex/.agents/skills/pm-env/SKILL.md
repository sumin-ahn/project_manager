---
name: pm-env
description: "PM 환경 관리 단일 스킬 — pm-config.sh facade wrap. repo add/list · repo protected(보호 브랜치 목록 조회/설정) · worktree add(→pm-bootstrap 바인딩 안내·--readonly 공유 슬롯) · slot status/release/remove · upstream show/switch(path↔URL). multi-PM 셋업·upstream 전환의 단일 진입. Triggers: 'pm-env', 'repo 추가', 'repo 목록', '보호 브랜치', 'protected 목록', 'worktree 추가', 'readonly 슬롯', 'slot 상태', '슬롯 제거', 'upstream 전환', '환경 관리'."
audience: user-entrypoint
---

# $pm-env — PM 환경 관리 (pm-config facade)

`./pm-config.sh`가 repo/worktree/slot/upstream 환경 셋업·조회의 CLI 계약이다.

> **Windows 진입**: `./pm-config.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-config.cmd`**(동일 인자·
> pm_import 가 루트로 복사). PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신
> **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## 인접 스킬

- **pm-env**: repo·worktree·slot·upstream 값 셋업/조회.
- [[pm-update]]: 현재 upstream으로 엔진 갱신. 여기서 값을 바꾸면 pm-update가 적용한다.
- [[pm-bootstrap]]: 세션 시작 시 슬롯 바인딩·상태점검.

## repo add

```bash
./pm-config.sh repo add <name> --git <url> --test "<cmd>" [--protected "main,develop"]
./pm-config.sh repo list                        # 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner)
```

`areas.md` 공유 레지스트리와 per-repo 셋업을 등록한 뒤 worktree 슬롯을 붙인다. `--protected`는 쉼표분리
보호 브랜치이며, 생략한 빈 칼럼은 **main/master/develop**으로 폴백한다. 사후 변경은 `repo protected`.

## repo protected

PM이 자율 commit/push할 수 없는 브랜치 목록. `areas.md`의 `protected` 칼럼이 단일 진실이고 훅
sidecar는 파생 캐시다.

```bash
./pm-config.sh repo protected <repo>                  # 조회 — 실효값 + 출처 + 훅 sidecar 정합
./pm-config.sh repo protected <repo> "main,release"   # 설정 — areas.md → 훅 sidecar 정합화
./pm-config.sh repo protected <repo> default          # 칼럼 비움 = main/master/develop 기본값 복귀
```

- 조회는 실효값·출처(명시/기본값 폴백/미등록)·훅 sidecar 정합의 3줄이다. 기본값 상태와 다른 clone 변경으로
  이 clone 훅만 낡은 drift를 구별하며, drift면 `⚠ 옛 목록(...)`과 재실행 안내가 붙는다.
- 설정은 **areas.md 먼저, 훅 sidecar 다음** 순서 고정. 변경은 board-git로 즉시 공유되고, 다른 clone은
  `$pm-bootstrap` 세션 시작 시 drift만 흡수한다.
- **"보호 없음"은 지정 불가**: 빈 문자열은 거부되고 `default`로 안내한다. 브랜치 실재는 검증하지
  않는다. 아직 없는 `release`의 선보호도 정상이지만 bare에 없으면 경고 1줄.
- `areas.md`에 같은 repo 행이 2개 이상이면 설정을 **부작용 없이 거부**한다. `board.py lint`도
  `areas-duplicate-repo`를 권고한다.

## worktree add

```bash
./pm-config.sh worktree add <repo> --user-ack <repo>              # 작업 슬롯(배타 대여·세션 바인딩)
./pm-config.sh worktree add <repo> --readonly --user-ack <repo>   # readonly 공유 슬롯(research 기준면)
```

물리 슬롯 생성은 사용자 승인 행위다. `--user-ack` 값이 대상 repo 와 정확히 같아야 통과하고,
없거나 다른 값이면 rc=1 로 거부된다(`--readonly`·`--task` 변형 포함). **세션(LLM)은 이 플래그를
스스로 부착하지 않는다** — 사용자에게 슬롯 생성 승인을 요청하는 것이 1순위다.

추가 후 **"이제 `$pm-bootstrap <repo> --slot N` 으로 이 슬롯에 바인딩하세요"**라고 안내한다.
이 바인딩 안내는 **사람(사용자) 대상**이며, 슬롯을 만든 세션이 자기 지시로 읽고 자동 실행하는
용도가 아니다(생성 직후 자기-할당은 차단 대상 사고 클래스다).

`--readonly`는 코드 읽기와 PM 홈 wiki(domain·architecture·status) 작성용 공유 기준면이다. detached
HEAD(released base), role=readonly, session/pid·배타 대여가 없다. 무소유 공유 자산이므로
**바인딩(`$pm-bootstrap --slot`)·release를 거부**하고, 갱신은 [[pm-worktree]] `refresh`만 허용한다
(set-base/rebase/dev/sync 거부). 제거는 `worktree remove --force`.

## slot status / release / remove

```bash
./pm-config.sh status | whoami        # 2축 cockpit — task 상황 + slot 풀·슬롯당 git 요약
./pm-config.sh release <slot> [--force]        # 작업완료 반납(idle 화·재사용) / --force=강제 백스톱
./pm-config.sh worktree remove <slot> [--force] # 슬롯 통째 제거(원자·번호 재사용)
```

- status의 **task 상황**은 task별 보유 작업공간(`work/<repo>_<N>`)·prefix(slot-모드 세션 제외), **slot
  풀**은 슬롯별 state·보유 task·role(work/readonly)와 `<branch>@<head> (base: <b>@<sha> · N behind)`
  요약이다. behind는 base 기록(`set-base`)이 있을 때만 계산하며, 미기록이면 `-`와 이유를 표시하고 자동
  추론하지 않는다. readonly는 branch `(detached)`와 base만 의미한다.
- `release`는 폴더를 유지해 idle로 재사용한다. `remove`는 `git worktree remove`, 슬롯 전용 브랜치
  (`<repo>_<N>`) 정리(머지 완료 시 삭제·미머지 보존·공유 브랜치 스킵), 장부 엔트리 삭제를 수행해 빈
  번호를 재사용한다. dirty/활성 리스는 거부하며 `--force`만 허용하고 dirty는 stash 보존한다.
- 제거 구분: 등록 슬롯=`worktree remove <slot>` / worktree 없는 dangling 장부=`worktree
  prune-stale` / 장부 없는 orphan worktree=사용자 `git worktree remove`. **사용자 명시 호출 전제**이며
  PM은 자율 제거하지 않는다.
- 미머지 전용 브랜치는 보존한다. 같은 번호 재생성은 `SlotBranchExists`로 중단되므로 브랜치를 머지/삭제
  후 재시도한다. 작업 재개는 **수동 checkout**(`git worktree add <슬롯경로> <브랜치>`·리셋 없음).
- 활성 슬롯을 `--force` 회수하면 stderr에 `⚠ 강제 회수`; dirty를 stash 보존하면
  `복구: git stash list/pop (공유 refs/stash)` 안내가 나온다.

## alloc / release --task / task end

task는 슬롯과 직교하는 작업스트림 정체성이다.

```bash
./pm-config.sh alloc <repo> --task <이름>          # idle 최소 번호 슬롯을 task 명의로 대여
./pm-config.sh release <slot> --task <이름>        # task 소유검사 후 반납(내 task 슬롯만)
./pm-config.sh task end <이름>                     # task 종료 — 소진 게이트 + 일괄 반납 + 아카이브
```

- `alloc`은 idle 최소 번호 슬롯을 task 명의(lease session)로 leased 전이한다. idle이 없으면 자동
  생성하지 않고 사용자에게 슬롯 생성 승인을 요청하라는 처방으로 멈춘다(승인 후 실행 형태는
  `worktree add <repo> --user-ack <repo>`). create/remove(물리층)는 사용자 승인,
  alloc/release(논리층)는 PM 자율. readonly는 대상이 아니다.
- `release --task`는 슬롯 session이 해당 task 명의가 아니면 거부한다. dirty도 거부하며,
  `--force`는 stash 보존 강제·소유검사 우회 백스톱이다. clean이면 폴더를 유지한 채 idle 반납한다.
- `task end`는 해당 task 명의의 claimed 티켓이 남으면 목록을 내고 거부한다. 사용자가 `board complete`
  또는 `board unclaim`(claimed→open)을 판단하며 자동 실행하지 않는다. 보유 작업공간 dirty도 목록과 함께
  거부한다. 모두 clean이면 슬롯을 삭제하지 않고 일괄 idle 반납, 장부 task 레코드 제거, 서술 폴더를
  `.local/tasks/_ended/<이름>-<날짜>/`로 이동한다. task prefix의 open 티켓은 정보만 표시하고 차단하지 않는다.

## task prefix

prefix는 task와 독립인 opt-in 분류 라벨이며 claim 경계가 아니다. 진행 중 지정·변경·해제할 수 있다.

```bash
./pm-config.sh task prefix <이름> <p>                    # 기존 카테고리 <p> 를 이 task 에 지정/변경
./pm-config.sh task prefix <이름> <p> --user-ack <p>     # 신규 카테고리 신설(사용자 승인값 결속)
./pm-config.sh task prefix <이름> none                   # 해제(무prefix·T-NNNN 로 발행)
```

- 포맷은 `[a-z0-9_]`; 그 외 rc1, 소문자 권장. `none`은 해제 리터럴. task 미존재면 rc1이며 생성은
  `$pm-bootstrap --task`에서만 한다.
- **신규 카테고리 신설은 사용자 명시 승인이 필요하다.** 대상 라벨이 기존 카테고리 4소스
  (areas.md prefix · 기발행 티켓 prefix · task 지정 prefix · solo local.conf)에 없으면 rc=1 로 거부하고
  현재 카테고리 목록과 승인 요청 처방을 낸다. 같은 게이트가 `board.py new --prefix`·
  `board.py init --prefix`·`board.py prefix rename|merge` 의 새 라벨에도 걸린다. 기존 카테고리
  사용(대소문자만 다른 표기 포함)은 무마찰이다. **세션이 스스로 `--user-ack` 을 붙여 카테고리를
  신설하지 않는다** — 승인 주체는 사용자다.
- `board.py new` 해소 순서: 명시 `--prefix` > task 지정 prefix > 기본 없음. task 명의
  (`--task <이름>`) 발행에 지정 prefix가 자동 적용되고 명시값이 1회 우선한다.
- prefix 카테고리 list/rename/merge는 별도 `board.py prefix`.

## upstream show / switch

```bash
./pm-config.sh upstream show
./pm-config.sh upstream set <url|path>
```

`set`은 검증 후 `local.conf upstream=`을 타 키 보존·fail-closed로 atomic 재기록한다. URL은
`git ls-remote` 도달성, 경로는 존재+checkout을 검증한다. https/ssh/file은 URL, 나머지는 경로로
판별하며 [[pm-update]]가 URL이면 cache clone, 경로면 pull한다.

## worktree add timeout 노브

대형 repo의 진행 중 checkout false-kill 방지용 3-layer 노브. 기본 30분.

- **엔진** `PM_GIT_TIMEOUT`(초·`none`/`0`/`unlimited`=무제한·기본 1800). console-visible runner이며
  `export PM_GIT_TIMEOUT=none`은 무제한; hang은 Ctrl-C.
- **claude 하네스** `BASH_DEFAULT_TIMEOUT_MS`(ms·출하 기본 1800000=30분)는 timeout 미지정 일반
  명령용, `BASH_MAX_TIMEOUT_MS`(ms·출하 기본 29300000=8시간 8분 20초)는 명시 timeout 상한이다.
  `.claude/settings.json` `env` 블록의 출하 기본이며 변경 후 세션을 재시작한다. cross 위임처럼
  장시간 예산이 필요한 호출은 Bash 툴에 MAX 이하 timeout을 명시한다.
- **opencode 하네스** `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`(ms). config나 `.env` 자동로드가
  없으므로 shell export:
  `export OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=29300000`
  (shell 프로파일/direnv `.envrc`). `EXPERIMENTAL`은 회사 버전에서 라이브 확인한다.

초과 시 엔진은 `터미널 직접 실행(PM_GIT_TIMEOUT=none)`을 안내한다. 변경 시 엔진 env·claude
settings.json·opencode shell export를 각각 조정한다.

## opencode stall 워치독

opencode `run` 스타트업 fetch가 첫 이벤트 없이 stall하면 호출층이 kill+재시도한다.

- `PM_OC_FIRST_EVENT_TIMEOUT`(기본 90초): 첫 json 이벤트까지 허용 시간.
- `PM_OC_STALL_RETRIES`(기본 2): 소진 시 fail-loud, 각 재시도는 stderr 1줄.

relay driver·pm_import `--fill auto`·release 라이브 테스트에 provider/원인 무관하게 적용된다.

backbone: `./pm-config.sh` → `pm_config.py`.
