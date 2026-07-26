---
name: pm-bootstrap
description: "PM 세션 시작 부트스트랩 — board 실측 / git 상태 / 회귀 / log 마지막 entry / 첫 turn 권장 액션 template 채움. backbone CLI .project_manager/tools/pm_bootstrap.py thin wrapper. Triggers: 'PM 부트스트랩', 'PM 세션 시작', '첫 turn 권장 액션', 'pm-bootstrap'."
audience: user-entrypoint
---

# /pm-bootstrap — PM 세션 시작 부트스트랩

> {{PROJECT_NAME}} PM 세션의 *기계 측정 + 인계 컨텍스트* 를 한 trigger 로 dump 한다 — board·git·회귀에
> 더해 **차수(`PM N차`) announce · log 마지막 handoff entry 본문 전체 · pm_state 남은작업/사용자발의 절**을
> 자동 surface 한다(self-sufficient·ADR-0035). PM 손은 *그 dump 를 요약·판단 / 옵션 제시 / 결정 요청* 만.
> backbone = `.project_manager/tools/pm_bootstrap.py`.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사전 부트스트랩 (skill 외부)

skill 호출 *전* PM 세션은 이미 다음을 읽어야 한다 (pm_role.md §부트스트랩):

1. `CLAUDE.md`
2. `.project_manager/wiki/pm_role.md` (정적 운영 매뉴얼)
3. 현재 정체성의 pm_state — **task 모드**는 `.project_manager/.local/tasks/<task>/pm_state.md`
   가 세션보다 오래 사는 연속성의 단일 앵커다. **slot 모드**는
   `.project_manager/.local/slots/<repo>_<N>/pm_state.md`(`<repo>_<N>` = worktree
   `work/<repo>_<N>` basename), 솔로는 `wiki/pm_state.md` legacy 폴백(T-0166/ADR-0033).
   신규 task는 호출 전 파일이 없는 것이 정상이며, backbone이 task를 장부에 생성하는 즉시
   pm_state도 함께 만든 뒤 같은 실행에서 읽는다. 사용자가 미리 만들거나 slot을 먼저 줄 필요가 없다.
   **부트스트랩이 현재 정체성의 파일에서 차수·남은작업을 자동 surface** 하니 손-read 는 보충일 뿐.
4. `.project_manager/wiki/status.md`
5. board 상태 — `python3 .project_manager/tools/board.py list` (board.md 는 파생 대시보드 · git-untracked — skill 이 자동 측정)
6. log/current.md 마지막 handoff entry — **부트스트랩이 본문 전체를 자동 dump** 한다(self-sufficient·ADR-0035). 직접 `python3 .project_manager/tools/pm_log.py tail` 은 baseline 재확인·더 넓은 범위 인용 시에만.

skill 은 *기계 측정* 만 자동화한다. 컨텍스트 인지·결정은 PM 의 몫.

## 실행

사용자 진입은 skill 하나다:

```text
/pm-bootstrap
```

backbone `.project_manager/tools/pm_bootstrap.py` 호출은 skill 내부에서 수행한다.

**multi-PM 모드 (멀티-PM·lean·T-0074)** — 사용자가 `/pm-bootstrap <repo> --slot <N>` 처럼 repo·슬롯을
주면, 그 인자를 그대로 엔진에 forward 한다:

```bash
python3 .project_manager/tools/pm_bootstrap.py --repo <repo> --slot <N>
```

이건 "나는 `<repo>_<N>` PM" *정체성 선언 + 상태점검* 이다 — 출력의 identity surface(세션=`<repo>_<N>`·
슬롯·라이브 브랜치·보드 공유) + 다른 활성 PM 현황을 받는다. **이후 이 세션은 보드/리스 조작에
`--repo <repo> --slot <N>` 을 명시**한다(정체성=대화 맥락·도구엔 명시 전달). 슬롯은 미리
`pm-config worktree add <repo>` 로 만들어 둔다. (솔로/무인자면 위 무-인자 dump 그대로.)

**task 모드 (일반 사용자 경로·작업 단위 정체성·T-0353)** — 사용자는 다음 한 줄로 신규 task를
시작하거나 기존 task를 재개한다(auto-task 없음):

```text
/pm-bootstrap --task <이름>
```

- backbone Python도 같은 task-only 계약이다:

  ```bash
  python3 .project_manager/tools/pm_bootstrap.py --task <이름>
  ```

- 장부에 없던 이름 → **신규 task 생성**. 작업공간 0개로 시작해도
  `.project_manager/.local/tasks/<이름>/pm_state.md`를 즉시 만든다.
- 기존 이름 → **resume**(보유 슬롯 집합 자동 수령·prefix 상태·task pm_state
  자동 복원). 구 엔진에서 state 없이 남은 task도 resume 시 즉시 backfill한다.
- task 진입은 **항상 `--task` 단독**이다. 사용자와 skill 모두 repo/slot을 지정하지 않으며,
  엔진도 `--task + --repo/--slot/--branch/--resume` 혼합을 거부한다. 작업공간 대여·편입은
  bootstrap이 아니라 `/pm-env alloc <repo> --task <이름>` 또는 task-aware worktree add가 맡는다.
- task-only 수집은 전역 auto-slot을 쓰지 않는다. 보유 0개면 PM 홈만, 1개면 그 작업공간,
  다중이면 보유 슬롯 전부를 freshness/opt-in 회귀 범위로 삼고 Git 대표 cwd는 정렬 첫 슬롯으로
  명시 surface 한다.
- 출력의 *task identity surface* 는 정체성 + **prefix 상태(기본=없음)** 를 보인다 — PM 이 사용자와
  확인한다(prefix 변경 명령은 후속 `task prefix`).
- 같은 task 를 **다른 창에서 이미 열고 있으면**(살아있는 세션) 거부한다("다른 창에서 열려 있음") —
  드문 2창 동시 열람을 막는다(비정상 종료면 자동 회수 후 재개 가능·회수 진입 시 "다른 창 작업중일 수
  있음" 경고를 surface).
- task 명은 **경로 문자(`/`·`\`·`..`·선행 `.`) 없는 단일 이름**이어야 하고, 자유 포맷이되
  `<등록 repo>_<N>`(슬롯 세션 예약 패턴)은 거부된다.

옵션:
- `--json` — JSON 출력 (다른 skill 의 wrapper 소비용).
- `--with-pytest` — 회귀 측정 opt-in (default 는 skip). 직전 handoff entry 의 회귀
  숫자가 의심스럽거나 baseline 재측정이 필요할 때만 사용. 별도 QA skill 이 wave 종료 시
  회귀 측정을 책임진다면 부트스트랩 단계 default 는 skip 으로 두는 게 합리적이다.

## 출력 해석 (PM 검증 항목)

CLI 가 markdown 표 dump:

- **board**: `done / open / claimed / blocked` — 전부 **내 세션 스코프**(ADR-0067: open=내 세션이 생성한 스트림·claim=내 세션). 타 세션분(open/claim)은 기본 dump 에 안 나온다 — 공유 풀 전체·타 PM 현황은 명시 조회 `board.py list --all`(전체)·`--mine`(내 전 세션) 로만. **board 숫자는 스냅샷 — 옵션 제시 전 `board.py list --mine` 으로 claim 주체를 교차 확인**(부분 push/오프라인 창).
- **회귀**: default 는 `(skip — handoff entry 참조 · --with-pytest 로 재측정)`.
  `--with-pytest` 명시 시 `N / N passed`. red 면 즉시 baseline fix 필요 (wave 시작 차단).
- **git**: 브랜치 + 최근 5 commit + working tree clean 여부. task-only면 task 소유 작업공간만
  수집하며 다중 슬롯의 대표 cwd와 전수 freshness 범위를 함께 표시한다.
- **차수**: 머리에 `## PM N차 부트스트랩` — task 모드는 task pm_state, slot 모드는 bound slot
  pm_state의 세션식별 절에서 자동 추론(미해소면 placeholder).
- **log/current.md 마지막 entry**: 제목(date·type·title) + **본문 전체** `<details>` dump. type=`handoff` 면 직전 PM 종료 정합 · `complete` 면 wave 진행 중일 수 있음.
- **pm_state 남은작업**: "남은 작업/사용자발의" 절을 surface.

## 잔여 PM 손작업

CLI 출력 뒤 PM 이 사용자에게 보고할 부분 — pm_role.md §"인계 후 PM 세션 첫 turn 의 권장 액션" template (차수·인계 본문·남은작업은 CLI 가 *이미 dump* 했으니 PM 은 **요약·판단**만):

1. **board 요약 1줄** — `done / open / claimed / blocked` 카운트 + 회귀·lint·git.
2. **직전 세션 요약 3~5줄** — CLI 가 dump 한 handoff entry 본문에서 핵심 산출물·메타 학습 *요약*(손-추출 아님).
3. **다음 옵션 N개** — CLI 가 surface 한 pm_state "남은 작업 전체 그림" 우선순위 인용.
4. **결정 요청** — *무엇부터 갈까요?* + 권장 시퀀스 1줄.

## 결정

- **thin wrapper** — skill 자체 비즈니스 로직 0·CLI 호출만. CLI 진화 시 skill 변경 0.
- **fail-soft 가 아니다** — CLI subprocess 실패 시 즉시 중단·PM 에게 보고 (red 신호).
- **자동 trigger 매칭** — frontmatter description 의 키워드로 사용자 한국어 명령 (*"부트스트랩"*) 시 자동 호출.

## 참고

- `.project_manager/tools/pm_bootstrap.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — 부트스트랩 절차 단일 진실
