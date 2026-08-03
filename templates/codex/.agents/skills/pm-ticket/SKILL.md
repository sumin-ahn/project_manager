---
name: pm-ticket
description: "티켓 authoring flow 자동화 — draft 발행(board.py new) → 본문 5절(목표/인터페이스/결정/DoD/참고) 자족성 채움 가이드 → 검증(placeholder 0) → board.py promote 승격. backbone CLI .project_manager/tools/board.py (new/lint/promote) thin wrapper·자체 로직 0. Triggers: '티켓 만들어', '티켓 발행', '새 티켓', 'ticket 초안', 'draft 승격', 'promote', 'pm-ticket'."
audience: pm-internal
---

# $pm-ticket — 티켓 authoring flow

PM이 draft 발행 → 본문 fill → promote를 실행하는 운영 스킬이다. backbone은 `.project_manager/tools/board.py`(`new`/`lint`/`promote`)이며 자체 로직은 없다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사용 시점

- 사용자가 새 작업을 티켓으로 만들거나 발행하라고 지시할 때.
- 설계/논의에서 나온 후속 작업을 board에 올릴 때.
- placeholder만 남은 draft의 본문을 채우고 승격할 때.

**무티켓/미충전 stub 발행 금지**. board-git 공유 형상에서 `new`는 미충전 본문을 `open/`이 아닌 draft로 격리하고, `promote`는 placeholder가 남은 승격을 차단한다.

## 1. draft 발행

```bash
python3 .project_manager/tools/board.py new "<한 줄 제목>" \
  [--touches <경로,경로>] [--depends <T-NNNN,...>] [--tag <tag,...>] \
  [--estimate small|medium|large] [--prefix <카테고리>]
```

- board-git 공유 형상: template placeholder가 남은 새 티켓은 자동으로 **draft**(`tickets/.drafts/`, board-git 미커밋)로 격리되어 다른 슬롯의 pull/handoff에 나타나지 않는다. 별도 flag는 없으며(`--draft` 없음) 미충전이면 draft다.
- solo/legacy(board 비-git) 형상: draft 격리가 없어 곧바로 `open/`에 발행된다.
- `--touches`: 작업 범위 및 다른 슬롯과의 충돌 평가용.
- `--prefix`: 작업 카테고리(자유 입력). multi-repo(등록 repo ≥2)에서는 필수다(네임스페이스).
- 출력의 `created <T-NNNN>`로 ID를 확인한다.

## 2. 본문 5절 자족성 채움

`board.py show <T-NNNN>`로 발행 파일 경로를 확인하고 5절을 실값으로 채운다. dev 서브에이전트가 본문만으로 추측 없이 구현할 수 있도록 실값 경로와 숨은 전제를 명시하고 placeholder를 0으로 만든다.

- **## 목표**: 무엇을 만들/바꿀/검증할지 1~3문장. `"무엇을 만들 / 바꿀 / 검증할지"` 뼈대 문장을 제거한다.
- **## 인터페이스**: 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격. `"이 ticket 이 만들거나 바꾸는 …"` 뼈대 문장을 제거한다.
- **## 결정**: 구현 방향 확정 사항(어떤 방식으로/왜). 미정은 `"열린 질문"`. `"구현 방향에 대한 확정 사항 …"` 뼈대 문장을 제거한다.
- **## 완료 조건 (DoD)**: 검증 가능한 산출물 체크리스트. `"핵심 산출물 (파일, 동작)"` 뼈대를 제거한다.
- **## 참고**: 실 경로/ADR/패턴 reference. `architecture 관련 절`·`xxxxx`·`T-XXXX`(패턴 reference)를 실값으로 교체한다. dangling wikilink는 lint가 차단하므로 실재 대상만 링크한다.
- **## 메모**: 완료 시 채우는 작업 저널이므로 비어도 정상이며 게이트 비대상이다.

## 3. 검증 → 승격

```bash
python3 .project_manager/tools/board.py lint            # placeholder/thin 잔존 확인(선택·조기 피드백)
python3 .project_manager/tools/board.py promote <T-NNNN>
```

- `promote`는 승격 전 본문을 재검사한다. placeholder(5절 뼈대 문장·`xxxxx`·`T-XXXX` 등)나 누락 표준 절이 있으면 **거부**(rc=1)하고 이슈를 나열한다. draft는 `.drafts/`에 남으므로 더 채운 뒤 재시도한다.
- 통과하면 draft → `open/` 이동 + board-git 커밋(공유 board에 등장) 후 claim 가능해진다.
- solo/legacy에서는 `promote`가 사실상 no-op(이미 open)이지만 검증은 `lint`로 실행한다.

## 규칙

- 발행·격리·검증·승격은 모두 `board.py`가 처리한다.
- `promote`는 `board.py new`와 같은 placeholder 검사(`_body_lint_issues`)를 재사용하며 목표/인터페이스/결정/DoD/참고 전부에 placeholder 0을 강제한다.
- 승격 전 draft는 로컬 `board.py show <id>`로만 조회된다.

## 참고

- `.project_manager/tools/board.py`: backbone CLI (`new`/`lint`/`promote`).
- `.project_manager/wiki/tickets/_template.md`: 5절 티켓 뼈대(목표/인터페이스/결정/DoD/참고).
