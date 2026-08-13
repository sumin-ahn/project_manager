---
name: pm-ticket
description: "티켓 authoring flow 자동화 — draft 발행(board.py new) → 본문 7절(목표/인터페이스/결정/설계/DoD/참고/메모) 자족성 채움 가이드 → 설계 단계(design 필드·claim 게이트) → 검증(placeholder 0) → board.py promote 승격. backbone CLI .project_manager/tools/board.py (new/lint/promote) thin wrapper·자체 로직 0. Triggers: '티켓 만들어', '티켓 발행', '새 티켓', 'ticket 초안', 'draft 승격', 'promote', 'pm-ticket'."
audience: pm-internal
---

# /pm-ticket — 티켓 authoring flow

PM이 draft 발행 → 본문 fill → promote를 실행하는 운영 스킬이다. backbone은 `.project_manager/tools/board.py`(`new`/`lint`/`promote`)이며 자체 로직은 없다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](../../.claude/skills/pm-ticket/references/operational-details.md)를 해당 상황에서 읽는다.

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
- solo/legacy(board 비-git) 형상: draft 격리가 없어 곧바로 `open/`에 발행된다.
- `--touches`: 작업 범위 및 다른 슬롯과의 충돌 평가용.
- `--prefix`: 작업 카테고리(자유 입력). multi-repo(등록 repo ≥2)에서는 필수다(네임스페이스).
- `--design`: 설계 단계 상태. 값은 `required` / `done` / `"waived: <사유>"` / `n/a` 네 형식이며(§3 표), 인식 불가한 값은 발행 전에 거부된다. 생략 시 `--estimate large`면 `required`, 그 외는 `n/a`로 박힌다. 설계 검증이 필요하다고 판단하면 small/medium에도 `--design required`로 명시 지정한다.
- 출력의 `created <T-NNNN>`로 ID를 확인한다.

## 2. 본문 7절 자족성 채움

`board.py show <T-NNNN>`로 발행 파일 경로를 확인하고 7절을 실값으로 채운다. dev 서브에이전트가 본문만으로 추측 없이 구현할 수 있도록 실값 경로와 숨은 전제를 명시하고 placeholder를 0으로 만든다.

- **## 목표**: 무엇을 만들/바꿀/검증할지 1~3문장. `"무엇을 만들 / 바꿀 / 검증할지"` 뼈대 문장을 제거한다.
- **## 인터페이스**: 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격. `"이 ticket 이 만들거나 바꾸는 …"` 뼈대 문장을 제거한다.
- **## 결정**: 구현 방향 확정 사항(어떤 방식으로/왜). 미정은 `"열린 질문"`. `"구현 방향에 대한 확정 사항 …"` 뼈대 문장을 제거한다.
- **## 설계**: `design: required`인 티켓만 채운다(3절). `n/a`·`waived`면 뼈대를 그대로 둬도 게이트 비대상이다.
- **## 완료 조건 (DoD)**: 검증 가능한 산출물 체크리스트. `"핵심 산출물 (파일, 동작)"` 뼈대를 제거한다. 완료 시 전항이 `- [x] <원문>` 또는 `- [>] <원문> (이월: <사유·귀속>)`이어야 `board.py complete`가 통과하므로(§`/pm-wave-finish`), 항목은 그 두 형태 중 하나로 마감할 수 있는 단위로 쓴다.
- **## 참고**: 실 경로/ADR/패턴 reference. `architecture 관련 절`·`xxxxx`·`T-XXXX`(패턴 reference)를 실값으로 교체한다. dangling wikilink는 lint가 차단하므로 실재 대상만 링크한다.
- **## 메모**: 완료 시 채우는 작업 저널이므로 비어도 정상이며 게이트 비대상이다.

## 4. 검증 → 승격

```bash
python3 .project_manager/tools/board.py lint            # placeholder/thin/design-pending 잔존 확인(선택·조기 피드백)
python3 .project_manager/tools/board.py promote <T-NNNN>
```

- `promote`는 승격 전 본문을 재검사한다. placeholder(5절 뼈대 문장·`xxxxx`·`T-XXXX` 등)나 누락 표준 절이 있으면 **거부**(rc=1)하고 이슈를 나열한다. 설계 절 뼈대는 이 5절 집합과 **별도 집합**이라 `design: required|done` 티켓에만 적용된다 — `n/a`·`waived`는 뼈대가 남아 있어도 통과한다. draft는 `.drafts/`에 남으므로 더 채운 뒤 재시도한다.
- `design: required`는 설계 절을 다 채웠어도 promote가 거부한다. **설계 검토 완료(`design: done` 또는 `waived`)가 open 진입 조건**이기 때문이다.
- 통과하면 draft → `open/` 이동 + board-git 커밋(공유 board에 등장) 후 claim 가능해진다.
- solo/legacy에서는 `promote`가 사실상 no-op(이미 open)이지만 검증은 `lint`로 실행한다.
