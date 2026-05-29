---
name: pm-bootstrap
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry / 첫 turn 권장 액션 template 채움. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
---

# /pm-bootstrap — PM 세션 시작 부트스트랩

> {{PROJECT_NAME}} PM 세션의 *기계 측정* 부분을 한 trigger 로 dump 한다. PM 손은
> *직전 세션 요약 / 옵션 제시 / 결정 요청* 만 채운다. backbone =
> `.project_manager/tools/pm_bootstrap.py`.

## 사전 부트스트랩 (skill 외부)

skill 호출 *전* PM 세션은 이미 다음을 읽어야 한다 (pm_role.md §부트스트랩):

1. `CLAUDE.md`
2. `.project_manager/wiki/pm_role.md` (정적 운영 매뉴얼)
3. `.project_manager/wiki/pm_state.md` (동적 상태 — 세션 window / 진행 중 의사결정 / 남은 작업)
4. `.project_manager/wiki/status.md`
5. `.project_manager/wiki/board.md`
6. `.project_manager/wiki/log/current.md` 마지막 handoff entry (의미 단위 — 직전 PM 이 읽기 범위 지정)

skill 은 *기계 측정* 만 자동화한다. 컨텍스트 인지·결정은 PM 의 몫.

## 실행

```bash
{{PY}} .project_manager/tools/pm_bootstrap.py
```

옵션:
- `--json` — JSON 출력 (다른 skill 의 wrapper 소비용).
- `--with-pytest` — 회귀 측정 opt-in (default 는 skip). 직전 handoff entry 의 회귀
  숫자가 의심스럽거나 baseline 재측정이 필요할 때만 사용. 별도 QA skill 이 wave 종료 시
  회귀 측정을 책임진다면 부트스트랩 단계 default 는 skip 으로 두는 게 합리적이다.

## 출력 해석 (PM 검증 항목)

CLI 가 markdown 표 dump:

- **board**: `done=N · open=N · claimed=N · blocked=N`. claimed > 0 면 *다른 세션 진행 중* — claim 충돌 회피.
- **회귀**: default 는 `(skip — handoff entry 참조 · --with-pytest 로 재측정)`.
  `--with-pytest` 명시 시 `N / N passed`. red 면 즉시 baseline fix 필요 (wave 시작 차단).
- **git**: 브랜치 + 최근 5 commit + working tree clean 여부. dirty 면 직전 핸드오프 commit 누락 확인.
- **log/current.md 마지막 entry**: date·type·title. type=`handoff` 면 직전 PM 종료 정합 · `complete` 면 wave 진행 중일 수 있음.

## 잔여 PM 손작업

CLI 출력 뒤 PM 이 사용자에게 보고할 부분 — pm_role.md §"인계 후 PM 세션 첫 turn 의 권장 액션" template:

1. **board 요약 1줄** — `done / open / claimed / blocked` 카운트 + 회귀·lint·git.
2. **직전 세션 요약 3~5줄** — log/current.md handoff entry 본문에서 핵심 산출물·메타 학습 추출.
3. **다음 옵션 N개** — pm_state.md "남은 작업 전체 그림" 의 우선순위 인용.
4. **결정 요청** — *무엇부터 갈까요?* + 권장 시퀀스 1줄.

## 결정

- **thin wrapper** — skill 자체 비즈니스 로직 0·CLI 호출만. CLI 진화 시 skill 변경 0.
- **fail-soft 가 아니다** — CLI subprocess 실패 시 즉시 중단·PM 에게 보고 (red 신호).
- **자동 trigger 매칭** — frontmatter description 의 키워드로 사용자 한국어 명령 (*"부트스트랩"*) 시 자동 호출.

## 참고

- `.project_manager/tools/pm_bootstrap.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 부트스트랩 절차 단일 진실
