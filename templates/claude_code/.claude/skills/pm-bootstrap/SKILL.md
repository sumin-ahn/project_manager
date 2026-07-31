---
name: pm-bootstrap
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry / 첫 turn 권장 액션 template 채움. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
audience: user-entrypoint
---

# /pm-bootstrap — PM 세션 시작 부트스트랩

`.project_manager/tools/pm_bootstrap.py`로 board·git·회귀, 차수(`PM N차`), 마지막 handoff 본문 전체, 현재 정체성의 pm_state 남은작업/사용자발의 절을 dump한다. PM은 결과를 요약·판단하고 옵션과 결정 요청을 제시한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사전 부트스트랩 (skill 외부)

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `/pm-bootstrap` dump 한 번뿐이다.
<!-- pm-bootstrap-preread:end -->

정식 계약은 `pm_role.md` §부트스트랩이 단일 진실이다. `architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는 시작 시 통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다. `architecture.md`는 현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 의도·실측과 충돌하면 기준으로 따른다. 바뀐 것은 읽는 시점뿐이다.

skill은 기계 측정만 자동화하며 컨텍스트 인지·결정은 PM이 한다.

## 실행

사용자 진입:

```text
/pm-bootstrap
```

backbone `.project_manager/tools/pm_bootstrap.py` 호출은 skill 내부에서 수행한다.

### multi-PM 모드 (멀티-PM·lean)

`/pm-bootstrap <repo> --slot <N>`의 repo·슬롯을 그대로 forward한다.

```bash
python3 .project_manager/tools/pm_bootstrap.py --repo <repo> --slot <N>
```

이는 `<repo>_<N>` PM 정체성 선언 + 상태점검이다. 출력의 identity surface(세션=`<repo>_<N>`·슬롯·라이브 브랜치·보드 공유)와 다른 활성 PM 현황을 확인한다. 이후 보드/리스 조작에 `--repo <repo> --slot <N>`을 명시한다. 슬롯은 먼저 `pm-config worktree add <repo>`로 만든다. 솔로/무인자는 무인자 dump를 쓴다.

### task 모드 (일반 사용자 경로·작업 단위 정체성)

auto-task 없이 신규 task를 시작하거나 기존 task를 재개한다.

```text
/pm-bootstrap --task <이름>
```

backbone도 task-only 계약이다:

```bash
python3 .project_manager/tools/pm_bootstrap.py --task <이름>
```

- 미등록 이름은 신규 task와 `.project_manager/.local/tasks/<이름>/pm_state.md`를 즉시 만든다(작업공간 0개 가능).
- 기존 이름은 resume하여 보유 슬롯 집합·prefix 상태·task pm_state를 복원한다. 구 엔진의 state 없는 task도 즉시 backfill한다.
- task 진입은 항상 `--task` 단독이다. 사용자/skill은 repo/slot을 지정하지 않고 엔진은 `--task + --repo/--slot/--branch/--resume` 혼합을 거부한다. 작업공간은 `/pm-env alloc <repo> --task <이름>` 또는 task-aware worktree add로 대여·편입한다.
- task-only 수집은 전역 auto-slot을 쓰지 않는다. 보유 0개면 PM 홈, 1개면 그 작업공간, 다중이면 모두를 freshness/opt-in 회귀 범위로 삼고 Git 대표 cwd는 정렬 첫 슬롯으로 surface한다.
- task identity surface의 정체성 + **prefix 상태(기본=없음)**를 사용자와 확인한다. 변경은 후속 `task prefix` 명령이다.
- 같은 task가 살아있는 다른 창에서 열려 있으면 `"다른 창에서 열려 있음"`으로 거부한다. 비정상 종료면 자동 회수 후 재개할 수 있고 회수 진입 시 `"다른 창 작업중일 수 있음"` 경고를 surface한다.
- task명은 경로 문자(`/`·`\`·`..`·선행 `.`) 없는 단일 자유 형식이어야 한다. `<등록 repo>_<N>` 슬롯 세션 예약 패턴은 거부한다.

옵션:

- `--json`: JSON 출력(다른 skill wrapper용).
- `--with-pytest`: 회귀 opt-in(default skip). 직전 handoff 회귀 숫자가 의심되거나 baseline 재측정 때만 사용한다. 별도 QA skill이 wave 종료 회귀를 맡으면 default skip을 유지한다.

## 출력 검증

- **board**: `done / open / claimed / blocked`는 내 세션 스코프(open=내 세션 생성 스트림, claim=내 세션)다. 타 세션분은 기본 dump에 없다. 공유 풀 전체는 `board.py list --all`, 내 전 세션은 `--mine`으로 조회한다. 숫자는 스냅샷이므로 옵션 제시 전 `board.py list --mine`으로 claim 주체를 교차 확인한다(부분 push/오프라인 창).
- **회귀**: default는 `(skip — handoff entry 참조 · --with-pytest 로 재측정)`. `--with-pytest`면 `N / N passed`; red면 즉시 baseline fix하고 wave 시작을 막는다.
- **git**: 브랜치, 최근 5 commit, working tree clean 여부. task-only는 task 소유 작업공간만 수집하고 다중 슬롯 대표 cwd·전수 freshness 범위를 표시한다.
- **차수**: `## PM N차 부트스트랩`. task pm_state 또는 bound slot pm_state의 세션식별 절에서 추론하며 미해소면 placeholder다.
- **마지막 entry**: `log/current.md` 제목(date·type·title) + 본문 전체를 `<details>`로 dump한다. `handoff`면 직전 PM 종료 정합, `complete`면 wave 진행 중일 수 있다.
- **pm_state**: 남은 작업/사용자발의 절을 surface한다.

## PM 보고

pm_role.md §인계 후 첫 turn template에 따라 CLI 출력 뒤 다음만 요약·판단한다:

1. board 1줄: `done / open / claimed / blocked` + 회귀·lint·git.
2. 직전 세션 3~5줄: dump된 handoff의 핵심 산출물·메타 학습.
3. 다음 옵션 N개: surface된 pm_state 남은 작업 전체 그림의 우선순위.
4. 결정 요청: *무엇부터 갈까요?* + 권장 시퀀스 1줄.

CLI subprocess 실패는 fail-soft가 아니므로 즉시 중단·보고한다. 이 skill은 비즈니스 로직 없는 thin wrapper이며 자동 trigger는 frontmatter description의 한국어 명령(예: `"부트스트랩"`)으로 매칭한다.

참고: `.project_manager/tools/pm_bootstrap.py`(backbone), `.project_manager/wiki/pm_role.md`(부트스트랩 절차 단일 진실).
