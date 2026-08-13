---
name: pm-bootstrap
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry / 첫 turn 권장 액션 template 채움. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
audience: user-entrypoint
---

# $pm-bootstrap — PM 세션 시작 부트스트랩

`.project_manager/tools/pm_bootstrap.py`로 board·git·회귀, 차수(`PM N차`), 마지막 handoff 본문 전체, 현재 정체성의 pm_state 남은작업/사용자발의 절을 dump한다. PM은 결과를 요약·판단하고 옵션과 결정 요청을 제시한다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 사전 부트스트랩 (skill 외부)

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `$pm-bootstrap` dump 한 번뿐이다.
<!-- pm-bootstrap-preread:end -->

정식 계약은 `pm_role.md` §부트스트랩이 단일 진실이다. `architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는 시작 시 통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다. `architecture.md`는 현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 의도·실측과 충돌하면 기준으로 따른다. 바뀐 것은 읽는 시점뿐이다.

skill은 기계 측정만 자동화하며 컨텍스트 인지·결정은 PM이 한다.

## 실행

사용자 진입:

```text
$pm-bootstrap
```

backbone `.project_manager/tools/pm_bootstrap.py` 호출은 skill 내부에서 수행한다.

### multi-PM 모드 (멀티-PM·lean)

`$pm-bootstrap <repo> --slot <N>`의 repo·슬롯을 그대로 forward한다.

```bash
python3 .project_manager/tools/pm_bootstrap.py --repo <repo> --slot <N>
```

이는 `<repo>_<N>` PM 정체성 선언 + 상태점검이다. 출력의 identity surface(세션=`<repo>_<N>`·슬롯·라이브 브랜치·보드 공유)와 다른 활성 PM 현황을 확인한다. 이후 보드/리스 조작에 `--repo <repo> --slot <N>`을 명시한다. 슬롯은 먼저 `pm-config worktree add <repo> --user-ack <repo>`로 만든다(물리 슬롯 생성은 사용자 승인
행위 — 세션이 `--user-ack`을 스스로 붙이지 않는다). 솔로/무인자는 무인자 dump를 쓴다.

### task 모드 (일반 사용자 경로·작업 단위 정체성)

auto-task 없이 신규 task를 시작하거나 기존 task를 재개한다.

```text
$pm-bootstrap --task <이름>
```

backbone도 task-only 계약이다:

```bash
python3 .project_manager/tools/pm_bootstrap.py --task <이름>
```

- 미등록 이름은 신규 task와 `.project_manager/.local/tasks/<이름>/pm_state.md`를 즉시 만든다(작업공간 0개 가능).
- 기존 이름은 resume하여 보유 슬롯 집합·prefix 상태·task pm_state를 복원한다. 구 엔진의 state 없는 task도 즉시 backfill한다.
- task 진입은 항상 `--task` 단독이다. 사용자/skill은 repo/slot을 지정하지 않고 엔진은 `--task + --repo/--slot/--branch/--resume` 혼합을 거부한다. 작업공간은 `$pm-env alloc <repo> --task <이름>` 또는 task-aware worktree add로 대여·편입한다.
- task-only 수집은 전역 auto-slot을 쓰지 않는다. 보유 0개면 PM 홈, 1개면 그 작업공간, 다중이면 모두를 freshness/opt-in 회귀 범위로 삼고 Git 대표 cwd는 정렬 첫 슬롯으로 surface한다.
- task identity surface의 정체성 + **prefix 상태(기본=없음)**를 사용자와 확인한다. 변경은 후속 `task prefix` 명령이다.
- 같은 task가 살아있는 다른 창에서 열려 있으면 `"다른 창에서 열려 있음"`으로 거부한다. 비정상 종료면 자동 회수 후 재개할 수 있고 회수 진입 시 `"다른 창 작업중일 수 있음"` 경고를 surface한다.
- task명은 경로 문자(`/`·`\`·`..`·선행 `.`) 없는 단일 자유 형식이어야 한다. `<등록 repo>_<N>` 슬롯 세션 예약 패턴은 거부한다.

옵션:

- `--json`: JSON 출력(다른 skill wrapper용).
- `--with-pytest`: 회귀 opt-in(default skip). 직전 handoff 회귀 숫자가 의심되거나 baseline 재측정 때만 사용한다. 별도 QA skill이 wave 종료 회귀를 맡으면 default skip을 유지한다.
