---
name: pm-ticket
description: "티켓 authoring flow 자동화 — draft 발행(board.py new) → 본문 5절(목표/인터페이스/결정/DoD/참고) 자족성 채움 가이드 → 검증(placeholder 0) → board.py promote 승격. backbone CLI .project_manager/tools/board.py (new/lint/promote) thin wrapper·자체 로직 0. Triggers: '티켓 만들어', '티켓 발행', '새 티켓', 'ticket 초안', 'draft 승격', 'promote', 'pm-ticket'."
audience: pm-internal
---

# /pm-ticket — 티켓 authoring flow 자동화

> {{PROJECT_NAME}} PM 이 손으로 하던 **티켓 authoring flow**(draft 발행 → 본문 fill → promote)를
> 한 trigger 로 처리한다 — 정체성만 채운 **얇은 래퍼**(pm-handoff 형·자체 로직 0). backbone =
> `.project_manager/tools/board.py`(`new`/`lint`/`promote`). PM 손은 *본문 5절 실값 서술* 만 남는다
> (그 서술이 이 flow 의 load-bearing 부분 — 나머지 발행/격리/승격/검증은 CLI 가 기계 처리).

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 세션 중 자동 invoke. 사용자가 자연어로 "이거 티켓으로 끊어"·
"새 티켓 만들어"·"초안 승격해" 라고 지시하면 PM 이 이 스킬을 부른다. 사용자 진입점(user-entrypoint)이
아니라 PM 의 **운영 도구** — 명령어化로 새로 느는 표면은 대개 pm-internal(ADR-0049).

## 사용 시점 (trigger)

다음 중 하나면 호출:
- 사용자가 새 작업을 티켓으로 끊자고 지시 (*"티켓으로 만들어"·"이거 발행해"*).
- 설계/논의에서 도출된 후속 작업을 board 에 올릴 때.
- placeholder 만 남은 draft 를 본문 채운 뒤 승격(promote)할 때.

**무티켓/미충전 stub 발행 금지** — 이 flow 는 그 규율의 기계화다. board-git 공유 형상에서 `new` 는
미충전 본문을 `open/` 이 아니라 격리된 draft 로 남기고, `promote` 게이트가 placeholder 잔존을
차단한다(빈 draft 는 승격 불가·T-0196/T-0366).

## 실행 — 3단계 flow

### 1. draft 발행

```bash
python3 .project_manager/tools/board.py new "<한 줄 제목>" \
  [--touches <경로,경로>] [--depends <T-NNNN,...>] [--tag <tag,...>] \
  [--estimate small|medium|large] [--prefix <카테고리>]
```

- board-git 공유 형상: 새 티켓은 template 뼈대(placeholder) 그대로라 자동으로 **draft**(격리·
  `tickets/.drafts/`·board-git 미커밋)로 발행된다 — 다른 슬롯의 pull/handoff 에 안 나타나 공유
  board 를 오염시키지 않는다(T-0196/T-0198). 별도 flag 불요(`--draft` 없음 — 미충전이면 곧 draft).
- solo/legacy(board 비-git) 형상: draft 격리 개념이 없어 곧장 `open/` 에 발행된다.
- `--touches` 는 작업 범위(다른 슬롯과 충돌 평가용)·`--prefix` 는 작업 카테고리(ADR-0042·자유 입력).
  multi-repo(등록 repo ≥2) 형상은 `--prefix` 필수(네임스페이스). 출력의 `created <T-NNNN>` 로 ID 확인.

### 2. 본문 5절 자족성 채움 (PM 손 — load-bearing)

발행된 티켓 파일(`board.py show <T-NNNN>` 로 경로 확인)의 **5절을 실값으로** 채운다. 자족성 =
*실값 경로·숨은 전제 명시·placeholder 0*(라이브 위임 프롬프트 수준 — dev 서브에이전트가 이 본문
하나로 추측 없이 구현할 수 있어야 한다·ADR-0049):

- **## 목표** — 무엇을 만들/바꿀/검증할지 1~3 문장. "무엇을 만들 / 바꿀 / 검증할지" 뼈대 문장 제거.
- **## 인터페이스** — 이 티켓이 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격. 뼈대
  문장("이 ticket 이 만들거나 바꾸는 …") 제거.
- **## 결정** — 구현 방향 확정 사항(어떤 방식으로/왜). 미정은 "열린 질문". 뼈대 문장("구현 방향에
  대한 확정 사항 …") 제거.
- **## 완료 조건 (DoD)** — 검증 가능한 산출물 체크리스트. "핵심 산출물 (파일, 동작)" 뼈대 제거.
- **## 참고** — 실 경로/ADR/패턴 reference 로 채운다. `architecture 관련 절`·`xxxxx`·
  `T-XXXX`(패턴 reference) 뼈대 토큰을 실값(예: `ADR-0049`·`T-0278`)으로 교체. dangling
  wikilink 는 lint 차단(ADR-0003)이니 실재 대상만 링크한다.
- **## 메모** 는 완료 시 채우는 작업 저널이라 비어도 정상(게이트 비대상).

### 3. 검증 → 승격

```bash
python3 .project_manager/tools/board.py lint            # placeholder/thin 잔존 확인(선택·조기 피드백)
python3 .project_manager/tools/board.py promote <T-NNNN>
```

- `promote` 는 승격 전 본문을 **재검사**한다 — placeholder(5절 뼈대 문장·`xxxxx`·`T-XXXX` 등)나
  누락 표준 절이 남아있으면 **거부**(rc=1)하고 남은 이슈를 나열한다. draft 는 `.drafts/` 에 그대로
  잔류하니 본문을 더 채운 뒤 재시도한다.
- 통과하면 draft → `open/` 이동 + board-git 커밋(공유 board 에 등장) → claim 가능한 상태가 된다.
- solo/legacy 형상은 draft 개념이 없어 `promote` 가 사실상 no-op(이미 open) — 그래도 검증은 `lint`
  로 돌린다.

## 결정

- **얇은 래퍼(비즈니스 로직 0)** — 발행/격리/검증/승격은 전부 backbone `board.py` 가 한다. CLI 가
  진화해도 이 스킬은 무변경(pm-handoff·pm-worktree 형·ADR-0049 명령어化 4요소·ADR-0051 live-HEAD).
- **fill 게이트 = 단일 깔때기** — `promote` 는 발행 게이트(`board.py new`)와 **같은** placeholder
  검사(`_body_lint_issues`)를 재사용한다(중복 판정 0·T-0196). 자족성 = placeholder 0 이 두 지점에서
  동일 규칙으로 강제된다(목표/인터페이스/결정/DoD/참고 절 전부).
- **draft 자동 격리** — 미충전 stub 이 board-git 공유 board 를 오염(다른 슬롯 handoff/bootstrap
  인계)시키던 클래스를 원천 차단(T-0198). 승격 전엔 로컬 `board.py show <id>` 로만 조회된다.

## 참고

- `.project_manager/tools/board.py` — backbone CLI (`new`/`lint`/`promote`).
- `.project_manager/wiki/tickets/_template.md` — 5절 티켓 뼈대(목표/인터페이스/결정/DoD/참고).
- 발행 규율·자족성 = pm_role.md §"티켓 authoring"·ADR-0049(명령어化 4요소)·ADR-0050(스킬 라이브
  하네스 테스트).
