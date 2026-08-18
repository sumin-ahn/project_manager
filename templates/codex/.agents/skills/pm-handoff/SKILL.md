---
name: pm-handoff
description: "PM 세션 종료 핸드오프 7단계 자동화 — log entry skeleton append + pm_state.md sliding window 정리 + 인계 프롬프트 stdout + 회귀 측정 + git status. backbone CLI .project_manager/tools/pm_handoff.py thin wrapper. Triggers: '핸드오프', '인계', 'PM 세션 종료', 'pm-handoff'."
audience: user-entrypoint
---

# $pm-handoff — PM 세션 종료 핸드오프 자동화

{{PROJECT_NAME}} PM 세션의 핸드오프 7단계를 한 trigger 로 처리한다. PM 손작업은 *log/current.md handoff entry 본문 서술 + 이번 세션 산출 경로를 pathspec 으로 명시한 git commit*이다. 인계 본문은 다음 세션 부트스트랩이 log entry 에서 dump한다. backbone = `.project_manager/tools/pm_handoff.py`.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

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
$pm-handoff
```

- 이 스킬은 **인자를 받지 않는다**. `$pm-handoff --task <이름>`·`$pm-handoff <아무 값>` 처럼 인자가 붙어 오면
  거부하고 인자 없이 다시 부르게 안내한다 — 스킬이 인자를 해석하지 않는다(codex 는 스킬 호출에 인자를
  전달하지 못하므로 카드가 인자에 기대면 그 하네스에서 동작하지 않는다).
- 종료 대상 정체성은 **부트스트랩에서 이미 확정돼 있다**(`$pm-bootstrap --task <이름>` 의 task identity
  surface 에서 사용자와 확인한 task 이름 · slot 모드는 `<repo>_<N>`). 종료 시 다시 받지 않는다.

PM 은 그 정체성으로 backbone 인자를 **스스로 채워** 호출한다(backbone 계약은 무변경 · `--task`·`--user-ack` 필수):

```bash
python3 .project_manager/tools/pm_handoff.py --task <부트스트랩 확인 이름> --user-ack <같은 이름>
```

`--user-ack` 값 = 부트스트랩에서 사용자와 확인한 정체성. 사용자의 `$pm-handoff` 호출이 그 정체성에 대한
명시 종료 지시이므로 값을 새로 받거나 추론하지 않는다. 부트스트랩 정체성이 확인되지 않은 세션(무인자
solo 등)만 실행 전 사용자에게 핸드오프 대상값을 확인한다.

엔진이 task pm_state에서 차수를 추론하고 기본 wave 요약과 task 보유 작업공간 집합을 해소한다. 사용자에게 repo/slot·session-seq를 받지 않으며, task와 repo/slot/branch/done의 혼합을 거부한다. 다음 세션 트리거는 `$pm-bootstrap --task <이름>` 하나다.

slot/솔로 모드에서 skill 내부 backbone 호출:

```bash
python3 .project_manager/tools/pm_handoff.py \
  --session-seq <N> \
  --wave-summary "<wave 1~3 한 줄 요약>" \
  --user-ack <값>
```

slot 모드의 `<값>`은 부트스트랩에서 확인된 canonical `<repo>_<N>`(legacy solo는 `solo`)을 PM 이
그대로 채운다(사용자 진입은 여기서도 `$pm-handoff` 무인자).

> 아래 `--session-seq` 설명은 slot/솔로 호환 경로에만 해당한다. 숫자만(`19`) 주면 CLI 가
> "차"를 붙여 `PM 19차`로 포맷한다.
> `19차` 를 줘도 CLI 가 후행 "차" 를 정규화(idempotent)해 이중부착(`19차차`)을 막는다.
> 차수는 `--session-seq` 로만 준다. multi-PM 이면 세션 정체성은
> canonical `--repo <repo> --slot <N>` 으로 준다.

옵션:
- `--dry-run` — log/current.md / pm_state.md 변경 미적용·stdout 미리보기만 (dirty 게이트도 판정 미리보기만·비차단).
- `--no-pytest` — 회귀 측정 skip (직전 wave 종결 commit 의 숫자 신뢰 시·**비권장**).
- `--ack-dirty "<사유>"` — [0/7] dirty-tree 게이트 명시 override. 사유 필수(개행은 공백으로 평탄화)·handoff entry 에 박제된다. 정상 경로는 override 가 아니라 **세션 산출을 먼저 커밋**하는 것이다.
- `--auto-trigger` — 사용자 명시 핸드오프 호출부를 위한 호환 신호. dirty 게이트를 차단 대신 loud 경고+사유 자동 박제로 강등하지만, 독자 트리거나 승인값이 아니며 `--user-ack`를 우회하지 않는다. 자동 배선 대상으로 광고하거나 세션이 자의로 붙이지 않는다.
- `--task <이름>` — task 모드의 정상 경로(**PM 이 부트스트랩 정체성으로 채운다 · 사용자 인자 아님**). 세션 종료 연속성 앵커를 slot→task로 이동하며, task 생성 시 만들어진 `.local/tasks/<이름>/pm_state.md`에 기록·dashboard 자기 섹션 `## <이름>`·log 헤더 태그 `(task:<이름>)`. lease는 유지한다(세션 종료 ≠ task 종료). 이름은 **공백·괄호·path 문자 없는 단일 토큰**(슬롯 예약 `<repo>_<N>` 불가)이다.
