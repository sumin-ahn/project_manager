---
name: pm-bootstrap
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry / 첫 turn 권장 액션 template 채움. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
audience: user-entrypoint
---

# $pm-bootstrap — PM 세션 시작 부트스트랩

`.project_manager/tools/pm_bootstrap.py`로 board·git·회귀, 차수(`PM N차`), 마지막 handoff 본문 전체, 이 task 의 열린 todo(ID·등록일·묵은 일수)를 dump한다. PM은 결과를 요약·판단하고 옵션과 결정 요청을 제시한다.

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
$pm-bootstrap --task <task>
```

`--task <task>`은 **필수**다 — 세션 정체성이 task 하나다. 슬롯은 정체성이 아니라 그 task가 빌린 작업 폴더이고, 인자 없이 부르면 엔진이 거부한다. skill은 받은 인자를 그대로 backbone에 넘긴다:

```bash
python3 .project_manager/tools/pm_bootstrap.py --task <task>
```

- 미등록 이름은 신규 task와 `.project_manager/.local/tasks/<task>/pm_state.md`를 즉시 만든다(작업공간 0개 가능).
- 기존 이름은 resume하여 보유 슬롯 집합·prefix 상태·task pm_state를 복원한다. 구 엔진의 state 없는 task도 즉시 backfill한다.
- 부트스트랩 인자는 `--task <task>`뿐이다. 작업공간은 `$pm-env alloc <repo> --task <task>` 또는 task-aware worktree add로 대여·편입한다.
- 수집 범위는 이 task가 보유한 작업공간뿐이다(다른 task 슬롯을 고르지 않는다). 보유 0개면 PM 홈, 1개면 그 작업공간, 다중이면 모두를 freshness/opt-in 회귀 범위로 삼고 Git 대표 cwd는 정렬 첫 슬롯으로 surface한다.
- task identity surface의 정체성 + **prefix 상태(기본=없음)**를 사용자와 확인한다. 변경은 후속 `task prefix` 명령이다.
- 같은 task가 살아있는 다른 창에서 열려 있으면 `"다른 창에서 열려 있음"`으로 거부한다. 비정상 종료면 자동 회수 후 재개할 수 있고 회수 진입 시 `"다른 창 작업중일 수 있음"` 경고를 surface한다.
- task명은 경로 문자(`/`·`\`·`..`·선행 `.`) 없는 단일 자유 형식이어야 한다. `<등록 repo>_<N>` 슬롯 폴더 이름 패턴은 거부한다.

옵션:

- `--json`: JSON 출력(다른 skill wrapper용).
- `--with-pytest`: 회귀 opt-in(default skip). 직전 handoff 회귀 숫자가 의심되거나 baseline 재측정 때만 사용한다. 별도 QA skill이 wave 종료 회귀를 맡으면 default skip을 유지한다.
