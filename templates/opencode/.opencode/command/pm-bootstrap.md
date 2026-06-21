---
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry 를 한 trigger 로 dump. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
---

<command-instruction>

# /pm-bootstrap — PM 세션 시작 부트스트랩

> {{PROJECT_NAME}} PM 세션의 *기계 측정* 부분을 한 trigger 로 dump 한다. PM 손은
> *직전 세션 요약 / 옵션 제시 / 결정 요청* 만 채운다. backbone =
> `.project_manager/tools/pm_bootstrap.py`. 비즈니스 로직 0 — 엔진 CLI 호출 thin wrapper.

## 사전 부트스트랩 (command 외부)

command 실행 *전* PM 세션은 이미 다음을 읽어야 한다 (pm_role.md §부트스트랩):

1. `AGENTS.md` (= opencode PM 부트스트랩·위임 규약·env 단일 진실)
2. `.project_manager/wiki/pm_role.md` (정적 운영 매뉴얼)
3. `.project_manager/wiki/pm_state.md` (동적 상태 — 세션 window / 남은 작업 · **per-clone 로컬**, pm-init 이 template 생성)
4. `.project_manager/wiki/status.md`

5·6 (board 상태·log 마지막 entry) 은 아래 CLI 가 자동 측정한다. 컨텍스트 인지·결정은 PM 의 몫.

## 실행

opencode bash tool 로 아래를 실행한다. 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일·C2 콘솔
reconfigure)하므로 env prefix 불필요 — Windows/CP949·PowerShell 서도 env 없이 동작. 구버전
Windows·서드파티 파이프서 드물게 필요하면 각 셸 문법으로(PowerShell `$env:PYTHONUTF8='1';`,
bash `PYTHONUTF8=1`).

```bash
{{PY}} .project_manager/tools/pm_bootstrap.py
```

**multi-PM 모드 (멀티-PM·lean·T-0074)** — 사용자가 `/pm-bootstrap <repo> --slot <N>` 처럼 repo·슬롯을
주면, 그 인자를 그대로 엔진에 forward 한다:

```bash
{{PY}} .project_manager/tools/pm_bootstrap.py --repo <repo> --slot <N>
```

이건 "나는 `<repo>_<N>` PM" *정체성 선언 + 상태점검* 이다 — 출력의 identity surface(세션=`<repo>_<N>`·
슬롯·라이브 브랜치·보드 공유) + 다른 활성 PM 현황을 받는다. **이후 이 세션은 보드/리스 조작에
`--session <repo>_<N>` 을 명시**한다(정체성=대화 맥락·도구엔 명시 전달). 슬롯은 미리
`pm-config worktree add <repo>` 로 만들어 둔다. (솔로/무인자면 위 무-인자 dump 그대로.)

옵션:
- `--json` — JSON 출력 (다른 command 의 wrapper 소비용).
- `--with-pytest` — 회귀 측정 opt-in (default 는 skip). 직전 handoff entry 의 회귀
  숫자가 의심스럽거나 baseline 재측정이 필요할 때만 사용.

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

- **thin wrapper** — command 자체 비즈니스 로직 0·CLI 호출만. CLI 진화 시 command 변경 0.
- **fail-soft 가 아니다** — CLI subprocess 실패 시 즉시 중단·PM 에게 보고 (red 신호).
- **자동 trigger 매칭** — frontmatter description 의 키워드로 사용자 한국어 명령 (*"부트스트랩"*) 시 자동 호출.

## 참고

- `.project_manager/tools/pm_bootstrap.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 부트스트랩 절차 단일 진실
- `AGENTS.md` — opencode PM 부트스트랩·엔진 호출 규약

</command-instruction>
