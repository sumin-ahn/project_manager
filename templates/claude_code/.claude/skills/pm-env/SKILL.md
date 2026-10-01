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
./pm-config.sh worktree add <repo> --user-ack <repo> [--folders <폴더,…>]  # 작업 슬롯
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
(rebase/dev/sync 거부). 제거는 `worktree remove <slot> --user-ack <slot> --force`.

## 슬롯 폴더 선택

작업 슬롯을 생성하거나 대여할 때 꺼낼 폴더를 정한다.

```bash
./pm-config.sh alloc <repo> --task <이름> [--folders <폴더,…>]
```

--folders는 Git cone sparse-checkout의 저장소 상대 폴더 목록이다. 생략하면 전체를 꺼내고, readonly와 함께 지정하면 부작용 전에 거부한다. Git 2.36 이상이 필요하며 잘못된 폴더·커밋 안 한 변경은 거부한다. 슬롯 반납은 폴더 규칙을 바꾸지 않는다. 좁힌 폴더 밖의 추적되지 않은 빌드 산출물은 Git이 지울 수 있으므로 alloc은 삭제 안내를 출력한다.

첫 sparse 적용은 공용 저장소의 extensions.worktreeConfig를 활성화하고 core.bare를 config.worktree로 옮긴다. 꺼낼 폴더 규칙은 해당 worktree의 설정으로 저장한다. status는 슬롯의 폴더 목록을 표시하고 JSON은 folders 목록 또는 null을 낸다. 폴더 제한 때문에 모든 슬롯에 없는 파일을 티켓 승격 인용으로 사용하면 실재 확인이 막힐 수 있다.

작업 중 필요한 폴더를 넓히려면 본인 슬롯에서 다음 두 명령을 순서대로 실행한다. 폴더를 줄이거나 전체로 돌리는 것은 다음 대여에서 정한다.

```bash
git -C <본인 슬롯 경로> sparse-checkout add <폴더…>
python3 .project_manager/tools/worktree_pool.py sync --repo <repo> --slot <N>
```

sync는 폴더 밖의 submodule을 제외하고 꺼냈으나 초기화되지 않은 경로를 받아온다. 이미 채워진 inactive·dev·dirty 작업은 보존한다. 별도 폴더 변경 엔진 명령을 만들지 않는다.

## slot status / release / remove

```bash
./pm-config.sh status | whoami        # 2축 cockpit — task 상황 + slot 풀·슬롯당 git 요약
./pm-config.sh release <slot> [--force]        # 작업완료 반납(idle 화·재사용) / --force=점유·소유검사 무시 백스톱
./pm-config.sh worktree remove <slot> --user-ack <slot> [--user-ack <브랜치>]... [--force]  # 슬롯 통째 제거(사용자 승인값·번호 재사용)
```

- status의 **task 상황**은 task별 보유 작업공간(`work/<repo>_<N>`)·prefix(slot-모드 세션 제외), **slot
  풀**은 슬롯별 state·보유 task·role(work/readonly)와 `<branch>@<head> (부모: <이름>@<sha> · 앞 N · 뒤 M)`
  요약이다. 부모 기록이 없으면 `(부모 기록 없음)`을 표시하고 추론하지 않는다. readonly는 branch
  `(detached)`와 따라갈 브랜치만 의미한다.
- status의 **슬롯 없는 보존 브랜치** 절(있을 때만 · `status --json`은 `preserved_branches`)은 슬롯 제거가 보존한
  브랜치(보호·기본 브랜치 제외)의 쓰던 슬롯·보존 시각·사유·부모·합쳐지지 않은 커밋 수다. 그 브랜치를 슬롯에 다시 체크아웃하면 빠진다.
- `release`는 폴더를 유지해 idle로 재사용한다. 커밋 안 한 변경이 있으면 `release`·`release --force` 모두
  거부한다. 엔진은 stash 하지 않는다 — 사용자에게 물어 커밋하거나, 사용자가 요청할 때만 stash 한다.
  `--force`는 점유·소유검사만 넘는다.
- `remove`는 사용자 승인 행위다. `--user-ack` 값에 입력한 슬롯이 그대로 없으면 rc=1 로 거부한다.
  **세션(LLM)은 이 값을 스스로 붙이지 않는다** — 사용자에게 슬롯 제거 승인을 요청하는 것이 1순위다.
  순서: 커밋 안 한 변경·git 미등록 트리 거부(`--force` 무관) → `git worktree remove` → 브랜치 삭제
  판정 → 장부 엔트리 삭제(빈 번호 재사용). `--force`는 점유(활성 리스)만 무시하고, 활성 슬롯을
  회수하면 stderr에 `⚠ 강제 회수`가 나온다.
- 브랜치 삭제 판정(슬롯 제거, 앞에서 걸리면 보존): 보호 브랜치·공용 저장소 기본
  브랜치 → 자식 브랜치 있음 → 부모 기록 없음·부모 브랜치 없음 → 부모에 합쳐지지 않은 커밋이 있고
  그 브랜치 이름의 승인값(`--user-ack <브랜치>`)이 없음(커밋 목록·origin 반영 여부 출력) → 그 밖은
  끝 커밋을 다시 확인한 뒤 삭제. 합쳐지지 않은 브랜치를 지울지는 **제거 전에** 사용자에게 묻는다
  (제거 뒤에는 같은 슬롯으로 다시 판정할 수 없다).
- 제거 구분: 등록 슬롯=`worktree remove <slot> --user-ack <slot>` / worktree 없는 dangling 장부=`worktree
  prune-stale` / 장부 없는 orphan worktree=사용자 `git worktree remove`. git 미등록 잔재 트리는 엔진이
  지우지 않는다 — 사용자가 경로를 확인·삭제한 뒤 `worktree prune-stale`.
- 보존된 전용 브랜치가 있으면 같은 번호 재생성은 `SlotBranchExists`로 중단된다. 작업 재개는
  **수동 checkout**(`git worktree add <슬롯경로> <브랜치>`·리셋 없음).

## upstream show / switch

```bash
./pm-config.sh upstream show
./pm-config.sh upstream set <url|path>
```

`set`은 검증 후 `local.conf upstream.path=`을 타 키 보존·fail-closed로 atomic 재기록한다. URL은
`git ls-remote` 도달성, 경로는 존재+checkout을 검증한다. https/ssh/file은 URL, 나머지는 경로로
판별한다. URL이면 엔진([[pm-update]])이 `<PM 홈>/.project_manager/.local/upstream-cache/<URL 해시>/`에
clone/fetch해 그 캐시를 소스로 쓰고, 경로면 그 checkout을 그대로 소스로 쓴다. 스킬이 판단하는 것은
로컬 경로 upstream의 pull 여부뿐이다.
