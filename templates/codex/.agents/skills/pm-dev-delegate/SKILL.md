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

## 사전 조건

- ticket 이미 claim (`$pm-wave-claim` 통과 · 세션 정체성 canonical `<repo>_<N>` · 솔로(M=1)는 생략).
- depends_on 모두 done.
- touches 명시.
- DoD verify-able.
- **컨텍스트 예산 확인** — touches 대형 파일·광범위 읽기 필요 시 dev truncation 위험. 미리 분할했거나 본문이 정확한 함수/라인·패턴 reference 로 dev 읽기를 좁히는지 확인 (안 되면 위임 전 본문 보강·분할).

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
- 슬롯 세션(비-task)·솔로(M=1)는 종전대로 — 이 주입은 task-mode 에서만.

## 라운드 파일 — Codex native prepare → spawn_agent → harvest

architect·developer·code-reviewer·researcher는 PM 홈 티켓을 직접 편집하지 않는다. native 위임마다
먼저 라운드 파일을 준비하고, `copy` 절대경로만 agent message에 넣은 뒤 종료 결과와 무관하게
harvest한다. 준비는 slot run-dir 에 **쓸 수 있는 파일 하나**(`NN-<역할>.md`)와 읽기 전용 입력
(`spec.md`=티켓 명세 · `rounds/`=이전 라운드)을 깐다.

```bash
python3 .project_manager/tools/pm_delegate.py ticket prepare \
    --ticket T-NNNN --role <architect|developer|code-reviewer|researcher> \
    --cwd <작업 worktree 절대경로>
# stdout JSON의 `copy`(라운드 파일 절대경로)만 agent message에 넣는다.

# 아래 역할별 spawn_agent 종료 뒤
python3 .project_manager/tools/pm_delegate.py ticket harvest \
    --copy <prepare JSON의 copy> --cwd <작업 worktree 절대경로>

# 미회수 준비 조회(컴팩션·세션 교체 뒤 복구 진입점)
python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested
```

prepare 실패 시 spawn하지 않는다. harvest 실패 시 같은 copy로 재실행하고 다음 단계로 넘기지 않는다.
회수 성공 = run-dir 삭제 = run 닫힘이라 재회수 개념이 없고, 산출이 시드 그대로면 board를 바꾸지 않고
경고만 내며 run-dir 을 남긴다(게이트 아님). 에이전트는 지정된 라운드 파일 하나만 쓰고 명세·이전
라운드는 읽기 전용으로 읽는다. code-reviewer native profile은 그 파일을 쓰도록 `workspace-write`지만
코드·board·git 수정은 금지이며, spawn 전후 `git status --short`·`git diff --name-only` 감사가 위반을
loud 표면화한다.

## cross-harness 위임 판정 (native 단락 · pm_delegate 채널 · ADR-0075)

역할 노동을 위임하기 전, 대상이 **내(PM) 하네스 네이티브로 도는지**(native) **다른 하네스 CLI 로
나가야 하는지**(cross)를 먼저 판정한다. 판정 1차 = 이 카드(PM)이고, `pm_delegate.py` 의 same-harness
경고는 백스톱(never-block·spike §3.6). 매핑은 `local.conf` 의 `delegate.<role>[.<tier>]` 키가 소유한다.

### 1. 매핑 조회 (dry-run — 미전송 미리보기)

역할이 어느 하네스·모델로 해소되는지 먼저 확인한다 (실 스폰 없음·외부 송신 0·rc=0):

```bash
python3 .project_manager/tools/pm_delegate.py --dry-run \
    --role <developer|researcher|architect|code-reviewer> \
    --prompt-file <task 프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard]
```

- 출력 = 해소된 `(harness, model, reasoning)` + 합성 프롬프트 + argv 미리보기. **전송하지 않는다**.
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

티어는 **`--tier hard`**(cross·pm_delegate) 또는 **native 경로의 hard 프로필**로 선택한다.
codex native 단락에선 난제를 `agent_type="developer-hard"`(`.codex/agents/developer-hard.toml`)로
spawn 한다(평시는 `agent_type="developer"`). 티어 매핑은 **하네스-중립** — 어느 하네스든 normal/hard
두 프로필을 가진다(claude 형상 예: `delegate.developer.harness=claude`·`.model=sonnet` /
`delegate.developer.hard.harness=claude`·`.model=opus`).

- **hard 프로필 미설정 = fail-loud(폴백 없음)** — `delegate.developer.hard.*` 가 없으면
  pm_delegate 는 `--tier hard` 를 normal 로 강등하지 않고 `rc=1` 로 거부한다(난제를 조용히 약한
  프로필로 돌리면 의도 왜곡·spike §3.2). native 경로도 동일 — hard 프로필(codex
  `developer-hard.toml`)이 없으면 명시 추가한다.

### 2. native 단락 판정

해소된 **target harness == 내(PM) 하네스**면 → **네이티브 위임**을 쓴다. codex PM 은 아래 §실행 패턴의
`spawn_agent` 네이티브 위임을 그대로 쓴다(다른 하네스가 PM 일 때는 각자의 네이티브 서브에이전트 위임).
pm_delegate 를 부르지 않는다 — 외부 송신 0·같은 프로세스 계열이라 더 저렴하다.

해소된 **target harness != 내 하네스**(cross)면 → 아래 §3 pm_delegate 호출.

### 3. cross 위임 실행 (pm_delegate.py)

target 이 다른 하네스면 `--dry-run` 을 떼고 실행한다 (opt-in 필요·외부 송신 발생):

```bash
python3 .project_manager/tools/pm_delegate.py --role <역할> \
    --prompt-file <프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard] [--ticket T-NNNN]
```

#### Codex egress 건별 승격 (load-bearing)

Codex 출하 기본은 `workspace-write` + `network_access=false`다. 이 경계는 일상 명령의
egress를 막지만 자식 `claude -p`/`opencode run` 역시 동일한 sandbox를 상속한다.
따라서 Codex PM의 cross 실위임은 아래 두 계층을 **항상 동반**한다.

1. 먼저 일반 sandbox에서 위 명령의 `--dry-run`을 실행한다. 해소 profile·argv·합성
   프롬프트·시크릿 판정을 확인하고, `Codex egress: escalation required`가 표시되는지
   본다. dry-run은 외부 송신·raw 예약·과금을 하지 않는다.
2. 승격이 필요한 실 명령은 Codex `exec_command` 호출에
   `sandbox_permissions="require_escalated"`와 기술적 network `justification`을 주고, 명령 argv에
   `--codex-egress-escalated`를 동시에 추가한다.

```text
exec_command(
  cmd="python3 .project_manager/tools/pm_delegate.py --codex-egress-escalated --role <역할> --prompt-file <절대경로> --cwd <절대경로> [--tier normal|hard] [--ticket T-NNNN]",
  workdir="<PM 홈 절대경로>",
  sandbox_permissions="require_escalated",
  justification="설정된 pm_delegate 수신자 호출에 필요한 network를 sandbox 밖에서 허용합니다.",
  prefix_rule=["python3", ".project_manager/tools/pm_delegate.py"],
)
```

`--codex-egress-escalated`는 권한을 만드는 플래그가 아니라 호출층 attestation이다. 단독으로
샌드박스 명령에 붙이지 말고 반드시 위 `sandbox_permissions` 메타데이터와 같이 쓴다.
최초 승인은 위의 좁은 reusable `prefix_rule`로 기억할 수 있다. Python 전체나 인자
전체를 prefix로 승인하지 마라. `delegate_enabled=true`는 설정된 profile의 외부 전송과
통상 과금에 대한 지속 의사표시이므로 PM은 후속 호출마다 비용을 다시 묻지 않는다.
Windows의 동일 좁은 prefix는 `prefix_rule=["py", ".project_manager/tools/pm_delegate.py"]`이며,
복사용 재실행 명령도 같은 `py + script` 2 token으로 시작해야 한다.
승인이 거절되거나 실행이 `rc!=0`/reply 미추출로 끝나면 그 역할 위임은 실패다.
사용자에게 보고하지 않고 native Codex/GPT로 무음 대체하지 마라. 대체는 사용자 지시
또는 이미 명시된 `fallback.*` tuple의 현행 규약으로만 한다. 전역
`sandbox_workspace_write.network_access=true`로 이 문제를 회피하지 마라.

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
- 결과: `rc=0` 성공(최종 reply = stdout·raw 는 파일 박제) / `rc=1` 실패(loud·raw 경로 stderr) /
  `rc=3` opt-in OFF. reply 를 회수해 PM 이 검토·board 갱신을 담당한다(위임 대상은 board 조작 안 함).
- **시크릿 스캔 차단 시 `--secret-scan-ack <digest>` 사용 규율**(T-0476): §4.7 이 합성 프롬프트를
  차단하면 **전 탐지 목록(발췌·판정·축) + 승인 토큰 + 재실행 커맨드**가 출력된다. **PM(LLM)이 반사적으로
  재실행하지 마라** — 그러면 게이트가 사실상 무력화된다. 규율: ① 전 탐지 발췌를 읽고 *시크릿을 논하는
  텍스트*(오탐)인지 *실 크리덴셜*(정탐)인지 판단 ② 조금이라도 모호하면 발췌를 **사용자에게 제시하고
  승인받은 뒤에만** ack ③ 정탐이면 ack 금지 — 프롬프트에서 해당 내용을 제거하고 재작성. 승인은 그
  프롬프트 전문+해소 수신자(harness:model)에 결속된 건별 1회다(1자 변경·수신자 변경 = 재승인).
  또한 **ack 로 통과한 실행은 폴백이 억제된다** — 승인이 primary 수신자에 결속돼 있어 폴백 수신자에게
  재승인 없이 승계될 수 없다. `delegate.<role>[.<tier>].fallback.*` 를 설정해 뒀어도 인프라 실패 시
  `rc=1` fail-loud 이며 억제 사유가 stderr·primary raw 양쪽에 남는다(폴백이 필요하면 `--harness/--model`
  로 수신자를 명시해 재실행하거나 ack 이 불필요하도록 프롬프트를 정리한다).

### opt-in 게이트 (외부 송신 · 기본 OFF)

cross 위임은 코드/프롬프트·worktree 내용을 **외부 하네스로 전송**한다 → `delegate_enabled` opt-in 이
꺼져 있으면(기본 OFF) pm_delegate 는 외부 하네스를 스폰하지 않고 `rc=3` 로 명시 거부한다. 켜기:

```ini
# local.conf (per-clone·git-ignored)
delegate_enabled = true
```

`=true` 는 "worktree 내용·(정제된) 환경이 타깃 하네스로 나갈 수 있음"을 사용자가 수용하는 계약이다
(과금·외부 송신·ADR-0004 상속). **native 단락(same-harness)은 이 게이트 밖** — 외부 송신이 없다.

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
     작업 위치(worktree 절대경로): <F6 해소 절대경로 — task-mode 시·슬롯/솔로는 생략>.

     ticket 본문은 python3 .project_manager/tools/board.py show T-NNNN 로 확인.
     등록 worktree 에 board 가 없으면 엔진이 worktree lease 장부로 단일 소유 PM 홈을 확정하고,
     read 명령이 요구하는 PM-owned 입력을 그 홈으로 **함께** 해소한다 — 첫 줄의
     `PM 입력 앵커: <PM 홈> (PM 홈 폴백: <입력 목록>)` 을 확인하라(`show` 는 `board`, `lint` 는
     `board·areas·wiki(...)·hooks·local.conf`). 장부 부재·손상·미등록이거나 여러 PM 홈이 같은 슬롯을
     등록하면 추측하지 않고 `[중단] 이 앵커에는 board가 없고 …` 로 멈춘다 — 그때는
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
     회귀 범위: <티켓 테스트 파일 목록> 만. **전체 회귀를 돌리지 마라** — 전량 검증은 릴리즈 절차
     1단계 1회다(병렬 wave 의 전체 회귀는 타 dev WIP 로 오염된 폐기 신호).

     완료 시 보고:
     - 변경 파일 목록
     - 열거한 인스턴스 목록과 각각의 처리
     - 신규 테스트 수
     - 지정 회귀 결과 (A passed · 범위 명시)
     - DoD 각 항목별 충족 evidence 명시""",
)
```

> ⚠ **kill 되어도 산출은 남는다 — 단 `pm_delegate`/`external_review` 실행에 한한다.**
> **cross-harness** 위임(`pm_delegate.py`)과 추가 리뷰(`external_review.py`)는 raw 를 실행 *전*에
> 공유 JSON 장부(`.project_manager/.local/raw_outputs.json`)에 등재하고 종료 시 감사 관측치
> (`rc`·`elapsed_sec`·`silence_sec`)로 마감한다. 백그라운드 호출이 끊겨 stdout(그 안의 raw 경로)을
> 잃어도 `python3 .project_manager/tools/pm_delegate.py raw [--unfinished]` 로 절대경로를 조회하라 —
> **미마감 레코드 자체가 kill 증거**다. 재위임 전에 반드시 확인한다(완성분을 버리고 중복 과금하는 경로).
> **native 위임은 장부에 남지 않는다** — 같은 하네스 안에서 도는 위임(각 하네스의 native
> 서브에이전트 경로)은 `pm_delegate` 를 경유하지 않으므로 이 조회 대상이 아니다. native 산출은
> 하네스 자체의 보고/전사에서 찾는다.
> **어느 장부를 봤는지 첫 줄로 확인한다** — `조회 장부: <절대경로>`. 장부는 **엔진 사본별**이라
> `경고: 다른 엔진 사본 장부가 있습니다(이 조회에서는 읽지 않음): <경로>` 가 뜨면 자동 대체 조회가
> 된 게 아니다 — 표시된 사본에서 **명시적으로 다시 조회**하라. `--output-dir DIR` 로 저장한 산출은
> `pm_delegate.py raw --output-dir DIR` 로 조회한다.

`spawn_agent`는 즉시 thread를 반환하므로 병렬 wave는 필요한 developer를 연속 spawn하고, 결과 의존
단계는 해당 thread의 완료 보고 뒤에 다음 spawn을 한다. custom `agent_type`과 full-history
`fork_turns="all"`은 함께 쓸 수 없다. 역할별 self-contained 프롬프트가 단일 진실이므로 기본은
`fork_turns="none"`; 꼭 필요한 최근 대화 맥락만 양의 정수(예: `fork_turns="3"`)로 제한한다.

### code-reviewer 위임

```
spawn_agent(
  agent_type="code-reviewer",
  fork_turns="none",
  task_name="orch_review_tnnnn",
  message="""T-NNNN 의 변경을 검토하라.

     라운드 파일(절대경로): <prepare JSON의 copy> — 이름은 `NN-code-reviewer.md`. 코드·board·git은
     수정하지 말고 판정 근거를 이 파일 하나에만 기록하라(첫 줄 헤더 유지). 같은 디렉터리의
     `spec.md`(티켓 명세)와 `rounds/`(이전 라운드)는 읽기 전용 입력이다.

     변경 파일: <touches 인자 그대로 인용>.
     작업 위치(병렬 wave 시 격리 스냅샷): <아래 §게이트 격리 스냅샷으로 만든 gate worktree
     절대경로>. 그 격리 스냅샷에서만 읽고 검토하라 — **공유 트리(dev 라이브 편집 중) 및 그 안에서의
     git 조작 금지**(checkout/stash/reset 등이 병렬 dev 의 WIP 를 덮는다). 솔로(비병렬)면 이 줄 생략.

     ⚠️ status.md / log/current.md 갱신은 orchestrator(PM) 담당 — 그 누락은 developer
     must-fix 아님.
     소환된 domain 페이지가 있으면 그 wiki DoD(touch∩covers 갱신·T-0081 soft step) 반영 여부도 점검.

     회귀 범위: <티켓 테스트 파일 목록> 만. **전체 회귀를 돌리지 마라**(전량 검증은 릴리즈 절차
     1단계 1회 — PM 담당). 범위 밖 실패를 이유로 반려하지 마라.
     (2라운드 이후) 확인 전용 판정 선행: 직전 must-fix 를 MF-n 별 해소/미해소/퇴행으로 먼저
     판정하라 — probe 재실행 실측값 포함. 신규 발견은 그 뒤에 NEW 라벨로 분리해 보고하라.

     완료 시 보고:
     - 같은 라운드 파일에 엔진이 시드한 리뷰 골격을 그대로 채운다. 필드 이름·분류·상태 낱말을
       스스로 만들거나 골격 밖 형식을 쓰지 않는다(스키마 단일 진실 = 엔진 파서). 미사용 array 도
       빈 배열로 둔다.
     - (2라운드 이후) 골격이 프리필한 확인 ID 를 먼저 채우고 신규 결함만 새 finding ID.
     - 설계 변경 제안은 확정하지 말고 골격의 설계-제안 분류로 보고한다.
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시""",
)
```

reviewer spawn 직전과 종료 직후 `git status --short`·`git diff --name-only`를 같은 worktree에서
대조한다. 역할 밖 변경이 있으면 회수 범위를 넓히지 말고 loud하게 보고한다. native reviewer의
`workspace-write`는 지정된 라운드 파일을 쓰기 위한 것이며 코드·board·git 수정 허가가 아니다.

### architect 위임·재설계

```
spawn_agent(
  agent_type="architect",
  fork_turns="none",
  task_name="orch_arch_tnnnn",
  message="""T-NNNN 의 설계 또는 재설계를 수행하라.

     라운드 파일(절대경로): <prepare JSON의 copy> — 이름은 `NN-architect.md`. 경계 실측·불변식·
     표면 상한·테스트 전략을 이 파일 하나에만 기록하라(첫 줄 헤더 유지). 재투입이면 같은 디렉터리
     `rounds/`의 이전 설계·developer·code-reviewer 라운드를 대조하고, 이번에 준비된 라운드 파일에
     결함과 변경 결정을 남겨라. `spec.md`·`rounds/`는 읽기 전용이다.""",
)
```

architect도 위 native `ticket prepare` 뒤 `spawn_agent`를 호출하고 종료 뒤 `ticket harvest
--copy ...`를 실행한다. 재설계는 새 prepare가 예약한 **다음 순번의 새 라운드 파일**에 쓰며 이전
라운드는 읽기 전용으로 남는다(라운드는 회수 후 불변).

> **fix 라운드 프롬프트는 PM 승인 delta만 쓴다.** PM은 라운드 파일 밖 명세의 PM 영역에
> `python3 .project_manager/tools/pm_delegate.py review disposition-template --ticket T-NNNN` 이 낸
> 판정 골격을 붙여 미판정 finding 을 전수 판정한 뒤
> `python3 .project_manager/tools/pm_delegate.py review delta --ticket T-NNNN`을 실행한다. 출력된
> delta 를 발췌하지 말고 그대로 developer에게 전달한다(끝의 제약 블록 포함).
> rejected/decision-required·보고서 전문은 출력에 없고 따로 전달하지도 않는다. 비성공이면 표시된 판정·재설계 처방을 먼저 수행하고, 빈 성공이면 재투입하지 않는다.
> cross fix 라운드는
> `pm_delegate --resume-from <T-NNNN>` 으로 **직전 dev 세션을 재사용**한다(cold 재투입은 티켓+코드
> 재섭취를 라운드마다 다시 낸다 — fresh 는 resume 미일치 폴백·전사 과대 시에만). 같은 accepted ID가 2라운드
> 연속 미해소면 라운드 추가가 아니라 재설계·분할로 전환한다(내부 라운드 상한 3 — `pm_playbook.md`
> §"라운드 프로토콜").

> ⚙️ `additional_reviewer_enabled=true` 로 추가 리뷰어(additional reviewer) 채널을 켠 채택자는
> reviewer 위임과 같은 시점에 교차검증을 돌린다. 기본은 OFF 이고, 끈 채택자에게 이 단계는 없다:
> `python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
> (ADR 본문 정합 필요 시 `--paths` 에 **코드 경로+ADR 함께 나열** — `--paths` 는
> `--ticket` touches 를 *대체*함). 상세는 `pm_playbook.md` §"추가 리뷰어 교차검증".
