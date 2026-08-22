---
name: pm-ticket
description: "티켓 authoring flow 자동화 — draft 발행(board.py new) → 본문 7절(목표/인터페이스/결정/설계/DoD/참고/메모) 자족성 채움 가이드 → 설계 단계(design 필드·claim 게이트) → 검증(placeholder 0) → board.py promote 승격. backbone CLI .project_manager/tools/board.py (new/lint/promote) thin wrapper·자체 로직 0. Triggers: '티켓 만들어', '티켓 발행', '새 티켓', 'ticket 초안', 'draft 승격', 'promote', 'pm-ticket'."
audience: pm-internal
---

# /pm-ticket — 티켓 authoring flow

PM이 draft 발행 → 본문 fill → promote를 실행하는 운영 스킬이다. backbone은 `.project_manager/tools/board.py`(`new`/`lint`/`promote`)이며 자체 로직은 없다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 사용 시점

- 사용자가 새 작업을 티켓으로 만들거나 발행하라고 지시할 때.
- 설계/논의에서 나온 후속 작업을 board에 올릴 때.
- placeholder만 남은 draft의 본문을 채우고 승격할 때.

**무티켓/미충전 stub 발행 금지**. board-git 공유 형상에서 `new`는 미충전 본문을 `open/`이 아닌 draft로 격리하고, `promote`는 placeholder가 남은 승격을 차단한다.

## 1. draft 발행

```bash
python3 .project_manager/tools/board.py new "<한 줄 제목>" \
  [--touches <경로,경로>] [--depends <T-NNNN,...>] [--tag <tag,...>] \
  [--estimate small|medium|large] [--prefix <카테고리>] \
  [--design 'required|done|"waived: <사유>"|n/a']
```

- board-git 공유 형상: template placeholder가 남은 새 티켓은 자동으로 **draft**(`tickets/.drafts/`, board-git 미커밋)로 격리되어 다른 슬롯의 pull/handoff에 나타나지 않는다. 별도 flag는 없으며(`--draft` 없음) 미충전이면 draft다.
- §3 본문 점검 라운드는 `pm_delegate.py ticket prepare|harvest --role architect`로 준비·회수한다 —
  **이 위임 경로만 규범이다**(PM이 자기 초안을 점검한 라운드는 독립 점검이 아니다). `board.py
  section-add <T-NNNN> --role architect`(슬롯 없는 준비 — PM이 직접 채우는 자리)는 hard draft의
  **설계**를 PM이 인라인으로 쓰는 용례다. 이 draft 경로는 board-git sync 0회이며 promote가
  출하를 소유한다. draft에서 developer/code-reviewer 라운드 준비는 거부된다. 라운드 디렉터리
  `tickets/rounds/<T-NNNN>/`는 티켓 상태 이동을 따라가지 않으므로 promote 뒤에도 같은 자리다.
- legacy(board 비-git) 형상: draft 격리가 없어 곧바로 `open/`에 발행된다.
- `--touches`: 작업 범위 및 다른 슬롯과의 충돌 평가용.
- `--prefix`: 작업 카테고리(자유 입력). multi-repo(등록 repo ≥2)에서는 필수다(네임스페이스).
- `--design`: 설계 단계 상태. 값은 `required` / `done` / `"waived: <사유>"` / `n/a` 네 형식이며(references 설계 단계 표), 인식 불가한 값은 발행 전에 거부된다. 생략 시 `--estimate large`면 `required`, 그 외는 `n/a`로 박힌다. 설계 검증이 필요하다고 판단하면 small/medium에도 `--design required`로 명시 지정한다.
- 출력의 `created <T-NNNN>`로 ID를 확인한다.
- `--touches`가 다른 활성/draft 티켓과 겹치면 stderr에 겹치는 경로별 티켓 목록 + 가용(idle) 슬롯 수를 낸다(겹침 0이면 완전 침묵). **차단하지 않는다** — 겹친다는 사실만으로 병합·직렬을 강제하지 않는다. 슬롯이 남아 있으면 그 겹침은 병렬로 나눠도 되는 신호일 수 있다.

## 2. 본문 7절 자족성 채움

`board.py show <T-NNNN>`로 발행 파일 경로를 확인하고 7절을 실값으로 채운다. dev 서브에이전트가 본문만으로 추측 없이 구현할 수 있도록 실값 경로와 숨은 전제를 명시하고 placeholder를 0으로 만든다.

- **## 목표**: 무엇을 만들/바꿀/검증할지 1~3문장. `"무엇을 만들 / 바꿀 / 검증할지"` 뼈대 문장을 제거한다.
- **## 인터페이스**: 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격. `"이 ticket 이 만들거나 바꾸는 …"` 뼈대 문장을 제거한다.
- **## 결정**: 구현 방향 확정 사항(어떤 방식으로/왜). 미정은 `"열린 질문"`. `"구현 방향에 대한 확정 사항 …"` 뼈대 문장을 제거한다.
- **## 설계**: `design: required`인 티켓만 채운다(references 설계 단계 절). `n/a`·`waived`면 뼈대를 그대로 둬도 게이트 비대상이다.
- **## 완료 조건 (DoD)**: 검증 가능한 산출물 체크리스트. `"핵심 산출물 (파일, 동작)"` 뼈대를 제거한다. 완료 시 전항이 `- [x] <원문>` 또는 `- [>] <원문> (이월: <사유·귀속>)`이어야 `board.py complete`가 통과하므로(§`/pm-wave-finish`), 항목은 그 두 형태 중 하나로 마감할 수 있는 단위로 쓴다.
- **## 참고**: 실 경로/ADR/패턴 reference. `architecture 관련 절`·`xxxxx`·`T-XXXX`(패턴 reference)를 실값으로 교체한다. dangling wikilink는 lint가 차단하므로 실재 대상만 링크한다.
- **## 메모**: 완료 시 채우는 작업 저널이므로 비어도 정상이며 게이트 비대상이다.

## 3. architect 점검 라운드 (초안 → 점검 → 비준)

본문을 채운 draft는 **PM 초안**이다. 승격 전에 architect 점검 라운드로 실측 대조를 받는다 — PM은 자기 초안의 리뷰어가 아니다(generate ≠ evaluate). 형식 게이트는 뼈대 잔존만 보므로 틀린 인용·티켓 간 충돌·범위 오판은 이 라운드에서만 잡힌다.

**Claude PM은 아래 실 실행 커맨드를 Bash 툴로 호출할 때 `timeout: 29300000`(ms)을 반드시
명시한다.** 이는 CLI `--timeout`(위임 turn 벽시계)이 아니라 호출층 Bash 툴 파라미터다.
`BASH_DEFAULT_TIMEOUT_MS=1800000`은 일반 무-파라미터 명령용이라 cross 위임에 의존하지 않는다.

```bash
python3 .project_manager/tools/pm_delegate.py ticket prepare --ticket <T-NNNN> --role architect --cwd <worktree>
# 위임(스킬 /pm-dev-delegate §architect 위임·본문 점검) 후:
python3 .project_manager/tools/pm_delegate.py ticket harvest --copy <prepare JSON의 copy> --cwd <worktree>
```

- draft × architect는 엔진이 이미 허용한다(claim 이전·board-git sync 0회). developer·code-reviewer의 draft 라운드는 거부된다. **PM이 채운 라운드는 이 점검을 충족하지 않는다** — promote는 역할명과 비시드 여부만 보므로 PM이 직접 쓰면 기계는 통과시키지만 초안자==점검자가 되어 3단이 무너진다. 본문 점검은 위 prepare/harvest 위임으로만 하고, `section-add`는 hard draft **설계** 인라인 작성에 쓴다(§1).
- 점검 항목: 본문이 인용한 `파일:줄`과 touches 경로의 실재, 다른 열린 티켓과의 충돌·의존(cross-module), **최소 수단**(기존 seam 재사용·삭제 대안·새 설정 키/플래그·서브커맨드가 정말 필요한지), 구현 가능하도록 인터페이스·DoD 보정.
- 회수된 라운드를 PM이 읽고 **바뀐 지점을 확인해 본문에 반영**한 뒤 승격한다(비준). 라운드는 회수 후 불변이므로 재점검은 다음 순번의 새 라운드다.
- `design: required|done` 티켓은 이 라운드가 회수·충전되기 전 `promote`가 rc=1로 거부한다. `n/a`·`waived`는 기계 강제 밖이지만, 다중 티켓을 한 번에 발행할 때와 `estimate: medium` 이상은 규범으로 점검을 받는다.

## 4. 검증 → 승격

```bash
python3 .project_manager/tools/board.py lint            # placeholder/thin/design-pending 잔존 확인(선택·조기 피드백)
python3 .project_manager/tools/board.py promote <T-NNNN>
```

- `promote`는 승격 전 본문을 재검사한다. placeholder(5절 뼈대 문장·`xxxxx`·`T-XXXX` 등)나 누락 표준 절이 있으면 **거부**(rc=1)하고 이슈를 나열한다. 설계 절 뼈대는 이 5절 집합과 **별도 집합**이라 `design: required|done` 티켓에만 적용된다 — `n/a`·`waived`는 뼈대가 남아 있어도 통과한다. draft는 `.drafts/`에 남으므로 더 채운 뒤 재시도한다.
- `design: required`는 설계 절을 다 채웠어도 promote가 거부한다. **설계 검토 완료(`design: done` 또는 `waived`)가 open 진입 조건**이기 때문이다.
- 형식과 함께 **본문이 주장하는 사실**도 재검사한다(새 플래그 없음). repo-relative `파일:줄` 인용의 파일 부재·줄 범위 초과와 `design: required|done` 티켓의 architect 점검 라운드 미회수는 **거부**(rc=1)이고, 실재하지 않는 `touches`는 **경고 1줄**(신설 예정 파일이 정상이라 차단하지 않는다)이다. 인용은 repo 루트 기준으로 먼저 찾고, 없으면 하위 디렉터리 기준 표기(`wiki/decisions/x.md`)로 보고 그 경로로 끝나는 파일을 소유 트리에서 찾는다 — 유일하게 해소되면 그 파일로 판정하고, 사본이 여럿이거나 basename만 적은 인용(`board.py:120`)은 어느 사본인지 확정할 수 없어 **판정불능 개수**로 표시된다(판정받으려면 repo 루트 기준 경로로 적는다). 어느 표기로도 실재 파일이 없으면 판정불능이 아니라 거부다.
- 통과하면 draft → `open/` 이동 + board-git 커밋(공유 board에 등장) 후 claim 가능해진다.
- legacy(board 비-git)에서는 `promote`가 사실상 no-op(이미 open)이지만 검증은 `lint`로 실행한다.
- 성공 시(승격 완료·board-git 기록 보류 두 경로 공통) `--touches` 겹침 재료가 §1과 같은 형식으로 stderr에 다시 나온다 — 발행 시점 이후 board가 바뀌었을 수 있어 승격 직전에 한 번 더 확인하는 자리다. 이 축도 **차단하지 않는다**.
