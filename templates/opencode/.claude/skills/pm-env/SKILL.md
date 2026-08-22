---
name: pm-env
description: "PM 환경 관리 단일 스킬 — pm-config.sh facade wrap. repo add/list · repo protected(보호 브랜치 목록 조회/설정) · worktree add(→pm-bootstrap 바인딩 안내·--readonly 공유 슬롯) · slot status/release/remove · upstream show/switch(path↔URL). multi-PM 셋업·upstream 전환의 단일 진입. Triggers: 'pm-env', 'repo 추가', 'repo 목록', '보호 브랜치', 'protected 목록', 'worktree 추가', 'readonly 슬롯', 'slot 상태', '슬롯 제거', 'upstream 전환', '환경 관리'."
audience: user-entrypoint
---

# /pm-env — PM 환경 관리 (pm-config facade)

`./pm-config.sh`가 repo/worktree/slot/upstream 환경 셋업·조회의 CLI 계약이다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

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
  `/pm-bootstrap` 세션 시작 시 drift만 흡수한다.
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

추가 후 **"이제 `/pm-bootstrap <repo> --slot N` 으로 이 슬롯에 바인딩하세요"**라고 안내한다.
이 바인딩 안내는 **사람(사용자) 대상**이며, 슬롯을 만든 세션이 자기 지시로 읽고 자동 실행하는
용도가 아니다(생성 직후 자기-할당은 차단 대상 사고 클래스다).

`--readonly`는 코드 읽기와 PM 홈 wiki(domain·architecture·status) 작성용 공유 기준면이다. detached
HEAD(released base), role=readonly, session/pid·배타 대여가 없다. 무소유 공유 자산이므로
**바인딩(`/pm-bootstrap --slot`)·release를 거부**하고, 갱신은 [[pm-worktree]] `refresh`만 허용한다
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

## upstream show / switch

```bash
./pm-config.sh upstream show
./pm-config.sh upstream set <url|path>
```

`set`은 검증 후 `local.conf upstream.path=`을 타 키 보존·fail-closed로 atomic 재기록한다. URL은
`git ls-remote` 도달성, 경로는 존재+checkout을 검증한다. https/ssh/file은 URL, 나머지는 경로로
판별하며 [[pm-update]]가 URL이면 cache clone, 경로면 pull한다.
