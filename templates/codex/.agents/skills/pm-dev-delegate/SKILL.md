---
name: pm-dev-delegate
description: "Codex native spawn_agent 기반 architect/developer/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
audience: pm-internal
---

# $pm-dev-delegate T-NNNN [--role architect|developer|code-reviewer] — orchestrator 위임

> {{PROJECT_NAME}} PM 의 Codex native `spawn_agent` 위임 표준 프롬프트. 역할은
> `agent_type="architect|developer|code-reviewer"`로 고르고, spawn 이 반환한 thread 는 비동기로 진행된다.
> ticket 본문이 self-contained 의무를 충족하면 위임 프롬프트는 한 줄이다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 클러스터 단계 표 (운영 단위 = 티켓 묶음)

운영 단위는 티켓 하나가 아니라 **묶음(클러스터)** 이다. 설계·리뷰·fix 는 묶음당 1회, 개발만
티켓당 1회다. 티켓 하나짜리 wave 도 **크기 1 클러스터**이고 같은 경로를 그대로 탄다(특례 없음).
**라운드 순번이 곧 단계**이며 묶음 멤버 전부가 같은 순번을 쓴다.

| 단계 | 실행 | 단위 | 라운드 순번 |
|---|---|---|---|
| 묶음 선언 | `board.py cluster new` | 묶음 1 | — |
| 설계 | `ticket prepare --cluster --role architect` | 세션 1 · 라운드 파일 N | `01-architect` |
| 구현 | `ticket prepare --cluster --role developer` | 티켓당 1 | `02-developer` |
| 리뷰 | `--role code-reviewer --cluster` | 세션 1 · 라운드 파일 N | `03-code-reviewer` |
| fix | `ticket prepare --cluster --role developer` | 묶음 브랜치 1명 | `04-developer` |
| 종결 | `rounds resolve --cluster` → `ticket_finish.py --cluster` | 묶음 1 | — |

```bash
python3 .project_manager/tools/board.py cluster new <이름> --tickets <T-NNNN,T-NNNN> --spike <설계 문서 경로>
python3 .project_manager/tools/pm_delegate.py ticket prepare --cluster <C-이름> --role architect --cwd <worktree 절대경로>
python3 .project_manager/tools/pm_delegate.py ticket prepare --cluster <C-이름> --role developer --cwd <worktree 절대경로>
python3 .project_manager/tools/pm_delegate.py --role code-reviewer --cluster <C-이름> --cwd <worktree 절대경로> --focus <검토 중점 파일> --background
python3 .project_manager/tools/pm_delegate.py cluster wait --cluster <C-이름> --cwd <worktree 절대경로>
python3 .project_manager/tools/ticket_finish.py --cluster <C-이름>
```

- 수열·테스트·종결의 규범은 [`pm_principles.md`](../../../.project_manager/wiki/pm_principles.md)
  §"티켓과 위임"만 소유한다. 이 카드는 위 표의 prepare/harvest 호출 절차만 제공한다.
- 현재 장부 단계는 `python3 .project_manager/tools/pm_delegate.py cluster show --cluster <C-이름>`으로 확인한다.
- **게이트 처분은 종결이 실행한다** — 종결 2단계가 `pm_delegate.py rounds resolve --cluster <C-이름> --pm-verified` 를 부르고 확인 커맨드도 엔진이 돌린다. PM 이 따로 부를 일은 처분만 먼저 확인할 때뿐이다.
- 묶음을 선언하지 않은 티켓은 발행이 만든 크기 1 장부에 귀속된다(stderr 1줄). 그 장부는 발행
  시점 코드 트리의 브랜치를 `base_branch` 로 싣고 묶음 브랜치(`branch`)만 비우므로, 종결의 머지
  단계가 `묶음 브랜치 미선언 — 무대상`으로 건너뛴다. `base_branch` 가 비면 재배치·머지는 무대상이
  아니라 정지다 — 판정 기준이 없기 때문이다. 묶음 브랜치까지 쓰려면 `board.py cluster new` 로
  선언한다 — 그 장부는 흡수된다.

## 사전 조건

- 묶음이 선언돼 있고 멤버 티켓이 claim 됐다 (`$pm-wave-claim` 통과 · 세션 정체성 canonical `<repo>_<N>`).
- 예외 — **draft의 architect 본문 점검 라운드는 claim 이전**이다. 엔진이 draft × architect만 허용하고(developer·code-reviewer의 draft 라운드는 거부) 이 경로는 board-git sync 0회다.
- depends_on 모두 done.
- touches 명시.
- DoD verify-able.
- **컨텍스트 예산 확인** — claim 전에 본문이 정확한 함수/라인·패턴 reference로 dev 읽기 범위를 좁혔는지 확인한다. 티켓 생성·목표 확대 판단은 [`pm_principles.md`](../../../.project_manager/wiki/pm_principles.md) §"티켓과 위임"을 참조한다.

## domain 소환 (recall — dev 위임 *전*)

위임 *전* ticket 의 covers 매칭 domain 페이지를 띄워 dev 에게 함께 넘긴다 (읽기 맥락 — dev 가 도메인 지식 없이 구현하는 걸 막음·ADR-0018 §7b):

```bash
python3 .project_manager/tools/domain.py affected --ticket T-NNNN
```

- 출력 = ticket touches ∩ 페이지 `covers` 매칭 페이지. 줄 앞 `⚠ ` = **stale**(담당 코드가 페이지 갱신 후 커밋됨).
- `(영향 domain 페이지 없음)` → 소환할 것 없음·생략.
- 매칭된 페이지 경로를 아래 developer 위임 프롬프트에 인용/전달한다. **⚠ stale 페이지는 "맹신 말 것"** 경고를 동반 — 담당 코드 변경 후 미갱신이라 정보가 상했을 수 있다(enforcement 아닌 visibility·Q3).

## task-mode 작업 위치 주입 (F6 해소 절대경로 · T-0355)

task-mode(v1.3.0 — 한 task 가 worktree 를 0개 이상 빌려 도는 모델) 위임에선 dev 가 **어느 worktree 에서** 구현할지를 PM 이 **F6 로 해소한 절대경로**로 위임 프롬프트에 명시 주입한다 (dev/git 이 짐작하지 않게 — cwd 는 해소에 비참여·T-0345). 해소값은 실행-위치 필요 도구가 그대로 surface 한다:

```bash
python3 .project_manager/tools/board.py regression run --task <이름>
# 출력: "regression: 작업공간(task <이름>) → <worktree 절대경로>"
```

- 그 `<worktree 절대경로>`를 developer 위임 프롬프트의 **작업 위치**로 박아 넣는다(짐작 제거).
- task 가 슬롯을 2개↑ 보유해 모호하면 F6 이 **에러**(⑦·암묵 선택 금지) — 쓰지 않는 잉여 슬롯을
  `python3 .project_manager/tools/pm_config.py release <slot> --task <이름>`으로 반납한 뒤 다시 해소한다.
- 슬롯 세션(비-task)은 종전대로 — 이 주입은 task-mode 에서만.

## 라운드 파일 — Codex native prepare → spawn_agent → harvest

architect·developer·code-reviewer·researcher는 PM 홈 티켓을 직접 편집하지 않는다. 단계마다 먼저
**묶음 멤버 전부의** 라운드 파일을 준비하고, `copy` 절대경로만 agent message에 넣은 뒤 종료 결과와
무관하게 harvest한다. 준비는 slot run-dir 하나에 티켓마다 **쓸 수 있는 파일 하나**
(`<티켓>/NN-<역할>.md`)와 읽기 전용 입력(`spec.md`=티켓 명세 · `rounds/`=이전 라운드)을 깐다.

```bash
python3 .project_manager/tools/pm_delegate.py ticket prepare \
    --cluster <C-이름> --role <architect|developer|code-reviewer|researcher> \
    --cwd <작업 worktree 절대경로>
# 티켓마다 stdout JSON 한 줄 — `copy`(라운드 파일 절대경로)만 agent message에 넣는다.

# 아래 역할별 spawn_agent 종료 뒤 — run-dir 을 주면 그 run 의 티켓 전부를 한 번에 회수한다
python3 .project_manager/tools/pm_delegate.py ticket harvest \
    --copy <prepare JSON의 copy 또는 그 run-dir> --cwd <작업 worktree 절대경로>

# 미회수 준비 조회(컴팩션·세션 교체 뒤 복구 진입점)
python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested
```

prepare 실패 시 spawn하지 않는다. harvest 실패 시 같은 copy로 재실행하고 다음 단계로 넘기지 않는다.
회수 성공 = run-dir 삭제 = run 닫힘이라 재회수 개념이 없고, 산출이 시드 그대로면 board를 바꾸지 않고
경고만 내며 run-dir 을 남긴다(게이트 아님). 거부 판정(역할·상태·예산·순서)은 **전부 예약 앞**이라
거부된 준비가 board 에 고아 라운드를 남기지 않는다. 에이전트는 자기 라운드 파일만 쓰고 명세·이전
라운드는 읽기 전용으로 읽는다. code-reviewer native profile은 그 파일을 쓰도록 `workspace-write`지만
코드·board·git 수정은 금지이며, spawn 전후 `git status --short`·`git diff --name-only` 감사가 위반을
loud 표면화한다. **`ticket prepare`는 역할이 native(하네스가 PM 하네스와 일치)일 때만 통과한다** —
cross 역할은 고아 시드를 막기 위해 rc≠0 으로 거부되며, `--ticket` 실 실행(아래 cross-harness 위임)이
자동 준비하라는 처방을 낸다.

## cross-harness 위임 판정 (native 단락 · pm_delegate 채널 · ADR-0075)

역할 노동을 위임하기 전, 대상이 **내(PM) 하네스 네이티브로 도는지**(native) **다른 하네스 CLI 로
나가야 하는지**(cross)를 먼저 판정한다. 판정 1차 = 이 카드(PM)이고, `pm_delegate.py` 의 same-harness
경고는 백스톱(never-block·spike §3.6). 매핑은 `local.conf` 의 `delegate.<role>[.<tier>]` 키가 소유한다.

### 1. 매핑 조회 (dry-run — 미리보기)

역할이 어느 하네스·모델로 해소되는지 먼저 확인한다 (실 스폰 없음·rc=0):

```bash
python3 .project_manager/tools/pm_delegate.py --dry-run \
    --role <developer|researcher|architect|code-reviewer> \
    --prompt-file <task 프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard]
```

- 출력 = 해소된 `(harness, model, reasoning)` + 합성 프롬프트 + argv 미리보기. **하네스를 부르지 않는다**.
- dry-run 은 opt-in 게이트를 **우회**한다(항상 rc=0 미리보기) — opt-in OFF 판정(`rc=3`)은 실 실행에서만 난다(아래 §opt-in 게이트).
- 매핑 미설정 역할은 `rc=1` fail-loud — `local.conf` 에 `delegate.<role>.harness/.model` 을 채운다(조용한 폴백 없음).
- `--tier` 는 **developer 전용** (난제=`hard`·평시=`normal`). 아래 §티어 판정 기준으로 고른다.
  비-개발 역할에 `--tier` 를 주면 usage error.

### 티어 판정 기준 (developer 전용 · 난제 상향 보수 규칙)

`--tier` 는 developer 위임에서만 고른다. PM 자의 해석을 줄이려 판정 기준을 아래로 못박는다
(mechanize 원칙) — 애매하면 **상향**한다(보수 기본값).

- **hard(난제)** — 아래 중 하나라도 해당: 엔진 코어 로직·파서/문법 변경·비파괴(하위호환) 계약·
  cross-module(여러 모듈 동시 변경)·보안 경계·**회귀 광범위**(넓은 blast-radius) 티켓.
- **normal(평시)** — 단일 모듈에 갇힌 변경·docs·기계적 sweep(rename·표기 통일)·테스트 추가·
  자명한 fix.
- **경계 애매 = hard 상향** — 어느 쪽인지 확실치 않으면 normal 로 내리지 말고 hard 를 고른다
  (난제를 약한 프로필로 돌리는 실패 비용 > 여유 프로필 비용).

티어는 **`--tier hard`**(cross·pm_delegate) 또는 **native 경로의 hard 카드**로 선택한다.
codex native 단락에선 난제를 `agent_type="developer-hard"`(`.codex/agents/developer-hard.toml`)로
spawn 한다(평시는 `agent_type="developer"`). 하드 카드는 **세 하네스가 모두 출하한다** —
claude `.claude/agents/developer-hard.md` · opencode `.opencode/agents/developer-hard.md` 가
같은 규칙의 등가물이다. 티어 매핑은 **하네스-중립** — 어느 하네스든 normal/hard
두 프로필을 가진다(claude 형상 예: `delegate.developer.harness=claude`·`.model=sonnet` /
`delegate.developer.hard.harness=claude`·`.model=opus`).

- **hard 프로필 미설정 = fail-loud(폴백 없음)** — `delegate.developer.hard.*` 가 없으면
  pm_delegate 는 `--tier hard` 를 normal 로 강등하지 않고 `rc=1` 로 거부한다(난제를 조용히 약한
  프로필로 돌리면 의도 왜곡). native 경로도 동일 — 하네스를 이유로 hard 를 normal 로
  대신하지 않으며, 카드가 없는 트리면 명시 추가한다.

### 2. native 단락 판정

해소된 **target harness == 내(PM) 하네스**면 → **네이티브 위임**을 쓴다. codex PM 은 아래 §실행 패턴의
`spawn_agent` 네이티브 위임을 그대로 쓴다(다른 하네스가 PM 일 때는 각자의 네이티브 서브에이전트 위임).
pm_delegate 를 부르지 않는다 — 같은 프로세스 계열이라 더 저렴하다.

해소된 **target harness != 내 하네스**(cross)면 → 아래 §3 pm_delegate 호출.

### 3. cross 위임 실행 (pm_delegate.py)

target 이 다른 하네스면 `--dry-run` 을 떼고 실행한다 (유료 호출):

```bash
python3 .project_manager/tools/pm_delegate.py --role <역할> \
    --prompt-file <프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard] [--ticket T-NNNN]
```

- `--prompt-file` — PM 이 만든 **self-contained task 프롬프트**를 담은 파일. 아래 §실행 패턴의 위임
  프롬프트 본문(developer/code-reviewer)을 그대로 파일로 저장해 넘긴다. 경로는 해소된 `--cwd` 하위
  또는 이 repo `.project_manager/` 하위만 허용(repo 경계 밖 = fail-loud·유출 차단).
- `--cwd` — dev 가 구현할 **작업 worktree 절대경로**. F6 해소값(task-mode 는 위 §task-mode 주입 절
  참조)을 실값으로 박는다. 모든 역할 필수(기본값 없음).
- `--tier` — developer 난제/평시(위 §1). 다른 역할엔 주지 않는다.
- **role preamble 은 엔진이 합성**한다 — 역할 정체성·금지사항(commit/push 등 git 비가역·board 조작·
  어댑터 디렉토리 `.claude/.codex/.opencode` 수정 금지)은 `pm_delegate.py` 의 role preamble 이 프롬프트
  앞에 자동 주입한다. 프롬프트 파일엔 **작업 내용만** 담고 금지 문구를 중복 서술하지 않는다.
- `--ticket`이 있는 architect·developer·code-reviewer 실 실행은 라운드 파일을 자동 준비하고 그
  절대경로 하나만 편집하라는 제한을 role preamble에 더한 뒤 `finally`에서 harvest한다.
  Codex cross named permission profile은 기존 격리를 보존한다. OpenCode target은 역할 카드가 없는
  adopter에서도 엔진 소유 런타임 role config와 `--agent <role>`로 exact 역할을 보존하며 default
  build/plan으로 강등하지 않는다. Claude·OpenCode처럼 단일 경로 쓰기
  격리를 보장하지 못해도 경고 후 사용자가 고른 target으로 계속 실행하며, 역할 규약과 위임 전후
  git/touches 감사가 범위 밖 변경을 loud하게 표면화한다. target 자동 대체나 reviewer 추가 opt-in은 없다.
- **병렬 wave** = PM 이 자기 하네스의 백그라운드 실행으로 pm_delegate 호출 자체를 병렬화한다.
  pm_delegate 는 동기·stateless — 병렬은 호출측 책임이다.
- 같은 세션이 claim 중인 다른 ticket과 `touches`가 겹치면(dry-run 포함) pm_delegate가 이미
  `=== 병렬 위임 touches 겹침 ===` 경고를 stderr에 낸다(never-block·처방: 순차 실행 또는 슬롯
  분리). 이 경고 하나만으로 "겹치니 직렬"로 판단하지 않는다 — `board.py new`/`promote`가 발행
  시점에 낸 가용(idle) 슬롯 수 재료를 함께 보고, 슬롯이 남아 있으면 순차 대신 슬롯 분리로
  병렬을 유지한다.
- 결과: `rc=0` 성공(최종 reply = stdout·raw 는 파일 박제) / `rc=1` 실패(loud·raw 경로 stderr) /
  `rc=3` 위임 스위치 off. reply 를 회수해 PM 이 검토·board 갱신을 담당한다(위임 대상은 board 조작 안 함).
### 위임 마스터 스위치

**위임자는 피위임자에게 자신과 같은 권한을 준다** — 위임 방향·하네스 조합과 무관하다(코덱스가 PM 일 때
클로드에게 위임하든, 오픈코드가 코덱스에게 위임하든 같다). 위임 경로에서 접근 권한·경로·env·볼 수 있는
내용을 좁히는 자리는 만들지 않는다. 남는 비대칭은 CLI 형식과 역할축(generate≠evaluate)뿐이다.

`delegate.enabled`는 "PM이 위임을 해도 되는가" 하나만 정한다. **기본은 허용**이고 채널(native/cross)로
갈리지 않는다 — 키를 지우면 허용, 명시적으로 끄려면 `false`:

```ini
# local.conf (per-clone·git-ignored)
delegate.enabled = false
```

끄면 세 층이 막는다: `pm_delegate` 실행 `rc=3` · `pm_delegate ticket prepare` `rc=3`(run-dir·라운드
순번 미생성) · 훅이 깔린 하네스의 역할 spawn `deny`. `ticket harvest`/`copies`와 `--dry-run`은 게이트
밖이다(진행 중 라운드가 고아가 되지 않게).

**차단 범위의 한계**: 훅 등록 파일(`settings.json`·`hooks.json`·`opencode.jsonc`)은 채택자 소유라
엔진이 전파하지 않고, 가드는 자기 고장 시 fail-open이다. 훅을 깔지 않았거나 가드가 고장난 형상에서는
티켓 없는 ad-hoc native spawn을 막지 못한다 — 스위치가 "모든 native를 막는다"고 읽으면 그것이
false-green이다.

## 실행 패턴

### developer 위임

```
spawn_agent(
  agent_type="developer",
  fork_turns="none",
  task_name="orch_dev_tnnnn",
  message="""T-NNNN 을 구현하라.

     라운드 파일(절대경로): <prepare JSON의 copy> — 이름은 `NN-developer.md`. 산출은 이 파일
     하나에만 쓰고(첫 줄 헤더 유지·그 아래 골격을 채움), 같은 디렉터리의 `spec.md`(티켓 명세)와
     `rounds/`(이전 라운드)는 읽기 전용으로만 읽어라. PM 홈 티켓은 편집하지 마라.

     세션명: orch-dev-TNNNN (board.py 조작은 orchestrator(PM) 담당·dev 는 코드+테스트만).
     작업 위치(worktree 절대경로): <F6 해소 절대경로 — task-mode 시·슬롯 세션은 생략>.

     ticket 본문은 python3 .project_manager/tools/board.py show T-NNNN 로 확인.
     등록 worktree 에 board 가 없으면 엔진이 그 트리의 `.git` 포인터에서 소유 PM 홈을 유도하고,
     read 명령이 요구하는 PM-owned 입력을 그 홈으로 **함께** 해소한다 — 첫 줄의
     `PM 입력 앵커: <PM 홈> (PM 홈 폴백: <입력 목록>)` 을 확인하라(`show` 는 `board`, `lint` 는
     `board·areas·wiki(...)·hooks·local.conf`). 유도가 안 되면(공용 Git 저장소를 못 읽거나, 그
     형상에서 홈을 못 찾거나, 찾은 홈에 `.project_manager` 가 없으면) 추측하지 않고
     `[중단] 소유 PM 홈을 확정할 수 없습니다: …` + rc1 로 멈춘다(read·mutation 같은 문구) — 그때는
     **프롬프트 요약만으로 진행하지 말고 즉시 PM 에게 보고**하라. board mutation 은 폴백하지 않으며
     PM 홈에서만 실행한다.
     본문이 self-contained — 목표/인터페이스/결정/DoD/참고 절 대로 구현.
     (PM 첨부 — 소환된 domain 페이지: <domain affected 출력 경로·있으면>. ⚠ 표시분은 stale 이니 맹신 말 것.)

     클래스 전수 열거 의무:
     - 구현 전에 결함 클래스의 인스턴스를 진입점·플랫폼·실패 모드·호출 경로 축으로 전수 나열해 보고.
     - 열거한 인스턴스 전부 처리. 보고된 형상만 처리한 결과는 미완.
     - 전수 열거가 불가능하면 불가능 사실과 열거 경계를 보고.
     - 스코프 확대 금지. 클래스는 해당 결함의 클래스에 한정하며 티켓 밖 기능은 포함 금지.

     역방향 확인 의무:
     - 고침이 반대 방향 실패를 만들지 않았는지 단언.
     - 느슨함을 조인 fix 는 과결속 확인.
     - 조임을 푼 fix 는 누락 확인.
     - 차단을 추가한 fix 는 정상 사용 차단 확인.

     검증 근거(PM 이 실값으로 지정): <무엇으로 재는지 — 실제 git 이 만든 산출물·설치 바이너리에서
     추출한 fixture·fake runner 아닌 층의 동작 단언. 미지정 금지 — cold dev 는 픽스처를 지어낸다.>
     inner-loop 회귀 범위: <티켓 테스트 파일 목록>. 단계 종료 때는 해소된 프로젝트 `test_cmd`를 직접
     실행하고 라운드 파일 `## 회귀`의 커맨드·`rc=0` 결과를 실값으로 채운다. 실행 횟수와 red 처리의
     규범은 `.project_manager/wiki/pm_principles.md` §티켓과 위임을 참조한다.

     완료 시 보고:
     - 변경 파일 목록
     - 열거한 인스턴스 목록과 각각의 처리
     - 신규 테스트 수
     - 지정 회귀 결과 (A passed · 범위 명시)
     - 단계 종료 전체 회귀 명령과 `rc=0` 결과
     - DoD 각 항목별 충족 evidence 명시
     - fix 라운드면 엔진이 시드한 검증 골격을 accepted ID 전수로 채운다(재현 커맨드·기대값·fix 전
       실값). 기계로 확인할 수 없는 항목은 골격이 제공하는 닫힌 사유로 선언한다. 형식·낱말은 골격이
       단일 진실이다.""",
)
```

> ⚠ **kill 되어도 산출은 남는다 — 단 `pm_delegate`/`additional_reviewer` 실행에 한한다.**
> **cross-harness** 위임(`pm_delegate.py`)과 추가 리뷰(`additional_reviewer.py`)는 raw 를 실행 *전*에
> 공유 JSON 장부(`.project_manager/.local/raw_outputs.json`)에 등재하고 종료 시 감사 관측치
> (`rc`·`elapsed_sec`·`silence_sec`)로 마감한다. 백그라운드 호출이 끊겨 stdout(그 안의 raw 경로)을
> 잃어도 `python3 .project_manager/tools/pm_delegate.py raw [--unfinished]` 로 절대경로를 조회하라 —
> **미마감 레코드 자체가 kill 증거**다. 재위임 전에 반드시 확인한다(완성분을 버리고 중복 과금하는 경로).
> **native 위임은 raw 장부에는 남지 않지만 라운드 파일에는 남는다** — 같은 하네스 안에서 도는 위임은
> 위 native prepare → spawn_agent → harvest를 수행한다. 하네스 보고/전사가 끊겨도 prepare가 출력한
> 라운드 파일을 먼저 읽고, `ticket harvest --copy ... --cwd ...`로 회수한 뒤에만 재위임한다. 경로마저
> 잃었으면 `ticket copies --unharvested`가 미회수 준비를 열거한다.
> **어느 장부를 봤는지 첫 줄로 확인한다** — `조회 장부: <절대경로>`. 장부는 **엔진 사본별**이라
> `경고: 다른 엔진 사본 장부가 있습니다(이 조회에서는 읽지 않음): <경로>` 가 뜨면 자동 대체 조회가
> 된 게 아니다 — 표시된 사본에서 **명시적으로 다시 조회**하라. `--output-dir DIR` 로 저장한 산출은
> `pm_delegate.py raw --output-dir DIR` 로 조회한다.

`spawn_agent`는 즉시 thread를 반환하므로 병렬 wave는 필요한 developer를 연속 spawn하고, 결과 의존
단계는 해당 thread의 완료 보고 뒤에 다음 spawn을 한다. custom `agent_type`과 full-history
`fork_turns="all"`은 함께 쓸 수 없다. 역할별 self-contained 프롬프트가 단일 진실이므로 기본은
`fork_turns="none"`; 꼭 필요한 최근 대화 맥락만 양의 정수(예: `fork_turns="3"`)로 제한한다.

### code-reviewer 위임 (묶음 1회)

리뷰 단위는 묶음이다. **격리 스냅샷·프롬프트 조립·라운드 자리 예약을 엔진이 한다 — PM 의 손 git 은
0이다.** PM 이 넣는 것은 검토 중점 문단 하나(`--focus`)뿐이다.

```bash
python3 .project_manager/tools/pm_delegate.py --role code-reviewer \
    --cluster <C-이름> --cwd <작업 worktree 절대경로> \
    --focus <검토 중점 문단 파일 절대경로> --background
python3 .project_manager/tools/pm_delegate.py cluster wait \
    --cluster <C-이름> --cwd <작업 worktree 절대경로>
```

- **리뷰 입력** = 장부의 통합 브랜치와 묶음 브랜치의 merge-base 이후 묶음 브랜치 변경 전부다.
  통합 브랜치가 그동안 앞서 갔어도 흡수분은 조상이라 빠진다.
- **격리 스냅샷**은 엔진이 저장소 밖에 만들고 실행 root 로 준다. 스냅샷 직전에 트리가 묶음 브랜치
  tip 인지 다시 결속하며, 리뷰 대상 파일에 커밋되지 않은 변경이 있으면 거부한다(프롬프트 diff 와
  모델이 읽는 파일이 갈리는 것을 막는다). 리뷰 뒤 스냅샷 정리도 엔진이 한다.
- **프롬프트**는 엔진이 조립한다 — 리뷰 단위·스냅샷 경로·입력 범위·변경 파일 목록·멤버 티켓 본문 N·
  PM 검토 중점이 실값으로 실린다. `--prompt-file` 은 이 경로에 주지 않는다.
- **산출 자리**는 run-dir 하나 안의 티켓별 `NN-code-reviewer.md` N개다. finding 은 그 티켓 파일에
  쓰고, 티켓 경계에 걸친 결함은 그 파일을 touches 로 소유한 티켓 파일에 쓴다. 스키마·다음 finding
  ID 실값은 엔진이 시드한 골격이 단일 진실이다(낱말을 손으로 만들지 않는다).
- `--background` 는 분리 세션으로 띄우고 즉시 반환한다. 회수 판정은 rc 가 아니라 라운드 회수
  상태이며 `cluster wait` 가 낸다 — 미회수 라운드가 있으면 rc≠0 이다.
- 검토 중점 문단에는 이 wave 에서 특히 볼 축을 적는다. `status.md`/`log/current.md` 갱신은 PM 담당
  이므로 그 누락은 developer must-fix 가 아니라는 점도 여기에 적는다.

### architect 위임 (묶음 1회)

설계도 묶음 단위다 — **세션 1 · 라운드 파일 N**. `ticket prepare --cluster --role architect` 가
멤버 전부에 `01-architect` 를 예약하고 run-dir 하나에 티켓별 자리를 깐다. 설계 단일 진실은 묶음
장부가 가리키는 설계 문서 하나이고, 티켓별 라운드 파일에는 그 티켓의 경계 실측·보정이 들어간다.

```
spawn_agent(
  agent_type="architect",
  fork_turns="none",
  task_name="orch_arch_cluster",
  message="""<C-이름> 묶음의 설계를 수행하라.

     라운드 파일(절대경로): <prepare JSON의 copy> — 티켓마다 하나이고 이름은 `NN-architect.md` 다.
     경계 실측·불변식·표면 상한과 developer가 실행할 필수 테스트의 대상·명령·기대값·음성 사례를
     각 티켓 자리에 기록하라(첫 줄 헤더 유지). `spec.md`·`rounds/`는 읽기 전용이다.""",
)
```

architect도 위 native `ticket prepare --cluster` 뒤 `spawn_agent`를 호출하고 종료 뒤
`ticket harvest --copy <run-dir>`를 실행한다. architect는 1회이고 회수 뒤 불변이다.

**본문 점검(draft·승격 전)** 도 같은 architect 호출을 쓴다 — 바꾸는 것은 프롬프트 본문 한 곳이다.
"묶음의 설계를 수행하라" 자리에 "각 티켓 초안 본문의 사실성을 점검하라. 새 설계가
아니라 **실측 대조**다" 를 넣고, 기록 항목을 지시한다: 본문이 인용한 `파일:줄`의 실재와 줄 범위 ·
touches 경로의 실재(소유 repo 좌표 기준) · 묶음 안팎 다른 열린 티켓과의 충돌·의존(cross-module) ·
최소 수단(기존 seam 재사용·삭제 대안·새 설정 키/플래그·서브커맨드가 정말 필요한지) · 구현
가능하도록 인터페이스와 DoD 보정. 항목마다 실행한 명령과 관측값으로 판정하고 틀린 주장은 대체
문구까지 적게 한다. 라운드 파일 경로와 읽기 전용 입력(`spec.md`·`rounds/`) 지시는 위 블록 그대로다.

이 라운드는 claim 이전 draft 에서 돌고, PM 은 회수된 보정 문구를 본문에 반영(비준)한 뒤 승격한다.
`design: required|done` 티켓은 이 점검 라운드가 회수·충전되기 전 `promote` 가 rc=1 로 거부한다.
설계 면제 값은 없다 — 설계가 몇 줄이면 몇 줄로 쓰고 `design: done` 으로 올린다.

> **fix 라운드는 묶음 1회이고 PM 승인 delta만 쓴다.** PM은 판정 블록을 손으로 적지 않는다 —
> `python3 .project_manager/tools/pm_delegate.py review disposition-template --cluster <C-이름>` 이
> 멤버 전부의 미판정 finding ID 를 프리필한 판정 골격을 내므로, 그 골격의 판정·사유 자리만 채워
> 각 티켓 명세의 PM 영역에 붙인 뒤
> `python3 .project_manager/tools/pm_delegate.py review delta --cluster <C-이름>`을 실행한다. 출력된
> delta 를 발췌하지 말고 그대로 developer에게 전달한다(끝의 제약 블록 포함).
> rejected/decision-required·보고서 전문은 출력에 없고 따로 전달하지도 않는다.
> fix 는 묶음 브랜치를 체크아웃한 슬롯에서 **developer 1명**이 accepted 전부를 해소한다
> (`ticket prepare --cluster --role developer` 가 `04-developer` 를 예약한다).
> fix developer가 해소한 프로젝트 `test_cmd`를 직접 실행하고 정확한 명령·`rc=0`을 라운드에
> 기록한다. harvest는 그 기록과 architect/reviewer targeted 계약만 검증하며 full을 재실행하지 않는다.
> cross fix 라운드는
> `pm_delegate --resume-from <T-NNNN>` 으로 **직전 dev 세션을 재사용**한다(cold 재투입은 티켓+코드
> 재섭취를 라운드마다 다시 낸다 — fresh 는 resume 미일치 폴백·전사 과대 시에만). 실패 처리는
> [`pm_principles.md`](../../../.project_manager/wiki/pm_principles.md) §"티켓과 위임"을 참조한다.

> ⚙️ 대상 튜플(`additional_reviewer.harness`/`.model`/`.reasoning`)을 선언한 채택자는 추가 리뷰어(additional reviewer) 로
> reviewer 위임과 같은 시점에 교차검증을 돌린다. 선언이 없는 채택자에게 이 단계는 없다:
> `python3 .project_manager/tools/additional_reviewer.py --ticket T-NNNN --adr ADR-NNNN`
> (ADR 본문 정합 필요 시 `--paths` 에 **코드 경로+ADR 함께 나열** — `--paths` 는
> `--ticket` touches 를 *대체*함). 상세는 `pm_playbook.md` §"추가 리뷰어 교차검증".
