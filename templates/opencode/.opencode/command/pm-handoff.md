---
name: pm-handoff
description: "PM 세션 종료 핸드오프 7단계 자동화 — log entry skeleton append + pm_state.md sliding window 정리 + 인계 프롬프트 stdout + 회귀 측정 + git status. backbone CLI .project_manager/tools/pm_handoff.py thin wrapper. Triggers: '핸드오프', '인계', 'PM 세션 종료', 'pm-handoff'."
audience: user-entrypoint
---

# /pm-handoff — PM 세션 종료 핸드오프 자동화

{{PROJECT_NAME}} PM 세션의 핸드오프 7단계를 한 trigger 로 처리한다. PM 손작업은 *log/current.md handoff entry 본문 서술 + 이번 세션 산출 경로를 pathspec 으로 명시한 git commit*이다. 인계 본문은 다음 세션 부트스트랩이 log entry 에서 dump한다. backbone = `.project_manager/tools/pm_handoff.py`.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](../../.claude/skills/pm-handoff/references/operational-details.md)를 해당 상황에서 읽는다.

## 사용 시점

- **사용자 명시 종료 신호가 유일한 트리거다** (*"세션 종료"·"인계해"*). PM 이 릴리즈 완결·wave
  종료를 근거로 자의 실행하지 않는다 — 종료 판단은 사용자 몫이며, 마감 시점이 왔다고 보이면
  실행이 아니라 제안한다.
- 컨텍스트 임계(넛지·stop 밴드)는 핸드오프 트리거가 **아니다** — 그 규약은 checkpoint 박제
  (`pm_log.py checkpoint`) 후 컴팩션 통과·세션 계속이다(compaction-native·stop 밴드도 비차단
  최종 넛지). 임계에서 이 스킬을 부르는 것은 구 계약의 오적용이다.

## 실행

사용자 진입은 **무인자**다:

```text
/pm-handoff
```

- 이 스킬은 **인자를 받지 않는다**. `/pm-handoff --task <이름>`·`/pm-handoff <아무 값>` 처럼 인자가 붙어 오면
  거부하고 인자 없이 다시 부르게 안내한다 — 스킬이 인자를 해석하지 않는다(codex 는 스킬 호출에 인자를
  전달하지 못하므로 카드가 인자에 기대면 그 하네스에서 동작하지 않는다).
- 종료 대상 정체성은 **부트스트랩에서 이미 확정돼 있다**(`/pm-bootstrap --task <이름>` 의 task identity
  surface 에서 사용자와 확인한 task 이름). 종료 시 다시 받지 않는다.

PM 은 그 정체성으로 backbone 인자를 **스스로 채워** 호출한다(backbone 계약은 무변경 · `--task`·`--user-ack` 필수):

```bash
python3 .project_manager/tools/pm_handoff.py --task <부트스트랩 확인 이름> --user-ack <같은 이름>
```

`--user-ack` 값 = 부트스트랩에서 사용자와 확인한 정체성. 사용자의 `/pm-handoff` 호출이 그 정체성에 대한
명시 종료 지시이므로 값을 새로 받거나 추론하지 않는다. 부트스트랩 정체성이 확인되지 않은 세션만
실행 전 사용자에게 핸드오프 대상값을 확인한다.

차수와 wave 요약은 **인자가 아니다** — 엔진이 회귀 게이트를 통과한 뒤 task pm_state 에서만
추론한다. 보유 작업 폴더 집합도 장부에서 자동 수령한다. 다음 세션 트리거는
`/pm-bootstrap --task <이름>` 하나다.

`--task` 를 생략하면 엔진이 `현재 폴더 → 장부의 대여 행` → `활성 task 가 하나뿐이면 그것` 순으로
스스로 해소하고, 그래도 정할 수 없으면 중단한다(쓸 자리를 지어내지 않는다). PM 은 부트스트랩에서
확인한 이름을 명시해 그 해소에 기대지 않는다.

옵션:
- `--dry-run` — log/current.md / pm_state.md 변경 미적용·stdout 미리보기만 (dirty 게이트도 판정 미리보기만·비차단).
- `--no-pytest` — 회귀 측정 skip (직전 wave 종결 commit 의 숫자 신뢰 시·**비권장**).
- `--ack-dirty "<사유>"` — [0/7] dirty-tree 게이트의 슬롯 밖(PM 홈 등) 미커밋 잔여 명시 override. 사유 필수(개행은 공백으로 평탄화)·handoff entry 에 박제된다. 정상 경로는 override 가 아니라 **세션 산출을 먼저 커밋**하는 것이다. 슬롯 쪽 커밋 안 한 변경은 넘지 않는다(`--force-handoff` 몫).
- `--auto-trigger` — 사용자 명시 핸드오프 호출부를 위한 호환 신호. dirty 게이트의 슬롯 밖 잔여를 차단 대신 loud 경고+사유 자동 박제로 강등하지만, 독자 트리거나 승인값이 아니며 `--user-ack`를 우회하지 않는다. 자동 배선 대상으로 광고하거나 세션이 자의로 붙이지 않는다.
- `--force-handoff` — task 가 보유한 작업 폴더에 커밋 안 한 변경이 있으면 [0/7] 게이트가 기록 전에 rc 1 로 멈추고 "커밋할까요? 강제 핸드오프할까요?" 를 사용자에게 물으라고 낸다. 사용자가 커밋을 고르면 커밋 뒤 다시 실행하고, 강제 핸드오프(컨텍스트·토큰 부족 등)를 고르면 이 플래그로 다시 실행한다 — 커밋 없이 핸드오프 기록만 하고 변경은 제자리에 두며 handoff entry 에 `커밋 안 한 변경 있음, 슬롯 유지: <slot>` 을 적는다. 세션이 자의로 붙이지 않는다.
- `--task <이름>` — 핸드오프 대상 task(**PM 이 부트스트랩 정체성으로 채운다 · 사용자 인자 아님**). 세션 종료 연속성 앵커이며 `.local/tasks/<이름>/pm_state.md` 에 기록·dashboard 자기 섹션 `## <이름>`·log 헤더 태그 `(task:<이름>)`. 작업 폴더 대여는 유지한다(세션 종료 ≠ task 종료). 이름은 **공백·괄호·path 문자 없는 단일 토큰**(슬롯 예약 `<repo>_<N>` 불가)이다.

## 이번 호출 인자

`$ARGUMENTS` — 사용자가 슬래시 명령 뒤에 준 값이다(하네스가 치환한다). 비어 있으면 인자 없이 부른 것이다. 이 카드가 인자를 받는 진입이면 그 값을 그대로 backbone 에 넘기고, 인자를 받지 않는 진입이면 값이 붙어 온 것을 사용자에게 알리고 인자 없이 다시 부르게 안내한다.
