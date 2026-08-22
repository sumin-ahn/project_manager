# $pm-env 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

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
- **신규 카테고리 신설은 사용자 명시 승인이 필요하다.** 대상 라벨이 기존 카테고리 3소스
  (areas.md prefix · 기발행 티켓 prefix · task 지정 prefix)에 없으면 rc=1 로 거부하고
  현재 카테고리 목록과 승인 요청 처방을 낸다. 같은 게이트가 `board.py new --prefix`·
  `board.py init --prefix`·`board.py prefix rename|merge` 의 새 라벨에도 걸린다. 기존 카테고리
  사용(대소문자만 다른 표기 포함)은 무마찰이다. **세션이 스스로 `--user-ack` 을 붙여 카테고리를
  신설하지 않는다** — 승인 주체는 사용자다.
- `board.py new` 해소 순서: 명시 `--prefix` > task 지정 prefix > 기본 없음. task 명의
  (`--task <이름>`) 발행에 지정 prefix가 자동 적용되고 명시값이 1회 우선한다.
- prefix 카테고리 list/rename/merge는 별도 `board.py prefix`.

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
