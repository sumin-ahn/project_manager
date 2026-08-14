---
name: pm-dev-delegate
description: "OpenCode task 기반 architect/developer/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
audience: pm-internal
---

# /pm-dev-delegate T-NNNN [--role architect|developer|code-reviewer] — orchestrator 위임

OpenCode native `task` tool + `description`·`subagent_type: architect|developer|code-reviewer`·`prompt` 필드. ticket 본문이 self-contained 의무 충족 시 위임 프롬프트는 한 줄.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 사전 조건

- ticket 이미 claim (`/pm-wave-claim` 통과 · 세션 정체성 canonical `<repo>_<N>` · 솔로(M=1)는 생략).
- depends_on 모두 done.
- touches 명시.
- DoD verify-able.
- **컨텍스트 예산 확인** — touches 대형 파일·광범위 읽기 필요 시 dev truncation 위험. 미리 분할했거나 본문이 정확한 함수/라인·패턴 reference 로 dev 읽기를 좁히는지 확인. 아니면 위임 전 본문 보강·분할.

## domain 소환 (dev 위임 전)

ticket 의 covers 매칭 domain 페이지를 띄워 dev 에게 전달한다:

```bash
python3 .project_manager/tools/domain.py affected --ticket T-NNNN
```

- 출력은 ticket touches ∩ 페이지 `covers`. `⚠ `는 담당 코드가 페이지 갱신 후 커밋된 **stale**.
- `(영향 domain 페이지 없음)`이면 생략.
- 매칭 경로를 developer 프롬프트에 인용한다. ⚠ stale 은 담당 코드 변경 후 미갱신이므로 **맹신 말 것** 경고를 붙인다(enforcement 아닌 visibility).

## task-mode 작업 위치 주입

task-mode(v1.3.0)에서는 dev 작업 worktree를 PM이 해소한 **절대경로**로 프롬프트에 넣는다. cwd는 해소에 참여하지 않는다:

```bash
python3 .project_manager/tools/board.py regression run --task <이름>
# 출력: "regression: 작업공간(task <이름>) → <worktree 절대경로>"
```

- 출력된 `<worktree 절대경로>`를 developer 프롬프트의 **작업 위치**에 넣는다.
- task가 슬롯 2개↑를 보유하면 에러(암묵 선택 금지). 잉여 슬롯을 `python3 .project_manager/tools/pm_config.py release <slot> --task <이름>`으로 반납하고 다시 해소한다.
- 슬롯 세션(비-task)·솔로(M=1)는 종전대로이며 이 주입은 task-mode만 적용.

## 위임 설정 조회와 transport 선택 (`pm_delegate`)

`local.conf`의 `delegate.<role>[.<tier>]` 매핑은 native와 cross 모두가 읽는 위임 설정의 단일
진실이다. 위임 전 target이 PM과 같은 하네스인지 판정해 같은 하네스면 native transport, 다른
하네스면 `pm_delegate.py` cross transport를 선택한다. native agent 카드의 모델은 conf와 일치해야
하며 가드가 불일치를 경고하되 spawn을 막거나 카드를 자동 수정하지 않는다. `delegate_enabled`는
cross 외부 송신·과금 동의만 게이트하며 native 설정 조회와 실행에는 적용하지 않는다.
1차 판정은 이 카드이며 `pm_delegate.py` same-harness 경고는 never-block 백스톱이다.

### 성장 티켓 사본 — 모든 위임의 준비/회수

developer·code-reviewer·architect는 PM 홈 티켓을 직접 편집하지 않는다. PM은 위임마다 그 시점
티켓 전문을 slot 안 기계 소유 사본으로 새로 준비하고, 에이전트는 프롬프트에 주입된 절대경로의
`pm-ticket-section:start/end role=<역할>` 자기 절만 채운다.

- **native prepare → spawn → harvest**:

  ```bash
  python3 .project_manager/tools/pm_delegate.py ticket prepare \
      --ticket T-NNNN --role <developer|code-reviewer|architect> \
      --cwd <작업 worktree 절대경로>
  # stdout JSON의 `copy`만 native task prompt에 주입한다.
  # `capability`는 PM이 harvest stdin용으로만 보관하며 agent prompt/파일/argv에 넣지 않는다.
  # native 위임 종료 뒤(rc/판정과 무관):
  python3 .project_manager/tools/pm_delegate.py ticket harvest \
      --copy <prepare JSON의 copy> --cwd <작업 worktree 절대경로> \
      --capability-stdin
  # 위 프로세스 stdin 한 줄에 prepare JSON의 capability를 입력
  ```

  prepare가 실패하면 spawn하지 않는다. harvest가 실패하면 티켓을 다음 단계로 넘기지 않고 같은
  `--copy`와 PM만 보관한 capability stdin으로 재실행한다. capability 값은 명령 argv·프롬프트·로그에
  쓰지 않는다. 사본·읽기 전용 baseline·metadata·tag 복제본은 성공 뒤에도 보존되며
  재호출마다 새 디렉터리를 쓰므로 서로 덮지 않는다.

- **cross 자동 후처리**: 아래 실 실행에 `--ticket T-NNNN`을 주면 `pm_delegate.py`가 prepare,
  사본 경로/자기 절 제한 preamble 합성, subprocess 실행, `finally` harvest를 한 호출에서 수행한다.
  하네스 rc 비정상·runner 예외에도 harvest를 시도하며, harvest 실패는 원래 rc보다 강한 비정상 종료와
  token 값 없는 진단을 stderr에 낸다. cross capability는 main 메모리에만 존재하고 raw·ledger·파일·
  프롬프트·stderr에 남기지 않으므로 프로세스 종료 뒤 수동 재-harvest할 수 없다. 실패 시 보존 사본을
  진단한 뒤 새 prepare/위임을 수행한다. resume delta, 세션 불일치 뒤 fresh retry, 인프라 fallback도
  이번 호출에서 준비한 **같은 사본 경로** 지시를 매 wire에 다시 싣고, 전 시도가 끝난 뒤 한 번 회수한다.
  `--dry-run`은 무부수효과라 사본을 만들지 않는다.

- **실패 판정**: marker 밖 byte 변경, marker 누락·중복·중첩·역할 불일치, 준비 뒤 같은 역할 절의
  PM 홈 변경은 stale overwrite 없이 rc=1. 원본과 사본을 보존한 채 원인을 고친 뒤 출력된
  `ticket harvest --copy ... --cwd ... --capability-stdin`를 capability stdin과 함께 다시 실행한다.
  사본 디렉터리는 repo-local
  `.git/info/exclude`에 기계 등록되어 `git status --short`와 커밋 대상에 나타나지 않는다. baseline과
  metadata·tag는 PM 홈 local trust 영역에도 복제한다. 어느 파일도 권위가 아니며, harvest는 파일에
  저장하지 않은 per-run 256-bit capability의 domain-separated HMAC(metadata canonical bytes+
  baseline)을 먼저 검증한 뒤에만 target/ordinal을 사용한다. 그 뒤 board lock 아래 canonical ticket
  재조회와 stale 비교까지 모두 통과해야 반영한다. 이미 원하는 block이 반영된 재-harvest도 sync를
  다시 시도하며 JSON의 `changed`와 `sync_ready`를 별도 반환한다. git ignore 등록은 linked
  worktree의 common git dir `info/exclude` 정확 좌표만 dirfd/nofollow/single-hardlink 경계에서
  append하며 이 안전 경계를 제공하지 못하면 외부 파일 write 없이 fail-closed한다.

### 1. 매핑 조회 (dry-run)

실 스폰·외부 송신 없이 하네스·모델을 확인한다(rc=0):

```bash
python3 .project_manager/tools/pm_delegate.py --dry-run \
    --role <developer|researcher|architect|code-reviewer> \
    --prompt-file <task 프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard]
```

- 출력: 해소된 `(harness, model, reasoning)` + 합성 프롬프트 + argv. 전송하지 않는다.
- dry-run은 opt-in 게이트를 우회하며 항상 rc=0 미리보기. opt-in OFF의 `rc=3`은 실 실행에서만 발생.
- 역할 매핑 미설정은 `rc=1` fail-loud. `local.conf`에 `delegate.<role>.harness/.model`을 채운다. 조용한 폴백은 없다. 단 `delegate.<role>[.<tier>].fallback.harness/.model[/.reasoning]`을 명시하면 **인프라 실패**(스폰 실패·한도·타임아웃·stall)에만 1단 폴백하고 사유를 stderr에 표기한다. 판정 반려/denylist 차단에는 발동하지 않는다. 권장 조합은 claude/opus. **예외 — `--secret-scan-ack`로 통과한 실행은 폴백이 발동하지 않는다**: ack 승인은 해소된 primary 수신자에 결속돼 있어 폴백 수신자에게 재승인 없이 승계될 수 없다. 인프라 실패 시 `rc=1`로 fail-loud하며 억제 사유가 stderr와 primary raw 양쪽에 남는다(폴백이 필요하면 `--harness/--model`로 수신자를 명시해 재실행하거나 ack이 불필요하도록 프롬프트를 정리한다).
- `--tier`는 developer 전용. 비-개발 역할에 주면 usage error.

### developer 티어

- **hard**: 하나라도 해당하면 선택 — 엔진 코어 로직, 파서/문법 변경, 비파괴(하위호환) 계약, cross-module(여러 모듈 동시 변경), 보안 경계, **회귀 광범위**(넓은 blast-radius).
- **normal**: 단일 모듈 변경, docs, 기계적 sweep(rename·표기 통일), 테스트 추가, 자명한 fix.
- 경계가 애매하면 **hard 상향**.

cross는 **`--tier hard`**, native는 hard 프로필(codex `developer-hard` agent 등)을 선택한다. 티어 매핑은 하네스-중립이며 각 하네스가 normal/hard 프로필을 가진다(예: `delegate.developer.harness=claude`·`.model=sonnet` / `delegate.developer.hard.harness=claude`·`.model=opus`).

**hard 프로필 미설정은 fail-loud·폴백 없음.** `delegate.developer.hard.*`가 없으면 normal로 강등하지 않고 `rc=1`로 거부한다. native도 hard 프로필(예 codex `developer-hard.toml`)이 없으면 명시 추가한다.

### 2. native/cross

- **target harness == PM 하네스**: 아래 실행 패턴대로 OpenCode native `task` tool을 호출한다. 실행 필드는 `description`·`subagent_type`·`prompt` 세 가지이며 다른 하네스의 background 필드를 요구하지 않는다. `pm_delegate`를 호출하지 않는다.
- **target harness != PM 하네스**: 아래 `pm_delegate` 호출.

### 3. cross 실행

target이 다른 하네스면 `--dry-run`을 떼고 실행한다(opt-in 필요·외부 송신):

**Claude PM은 아래 실 실행 커맨드를 Bash 툴로 호출할 때 `timeout: 29300000`(ms)을 반드시
명시한다.** 이는 CLI `--timeout`(위임 turn 벽시계)이 아니라 호출층 Bash 툴 파라미터다.
`BASH_DEFAULT_TIMEOUT_MS=1800000`은 일반 무-파라미터 명령용이라 cross 위임에 의존하지 않는다.

```bash
python3 .project_manager/tools/pm_delegate.py --role <역할> \
    --prompt-file <프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard] [--ticket T-NNNN]
```

- `--prompt-file`: self-contained task 프롬프트 파일. 아래 developer/code-reviewer 위임 본문을 그대로 저장한다. 해소된 `--cwd` 하위 또는 이 repo `.project_manager/` 하위만 허용하며 repo 밖은 fail-loud·유출 차단.
- `--cwd`: 구현할 worktree **절대경로**. 모든 역할 필수(기본값 없음). task-mode 해소값을 실값으로 넣는다.
- `--tier`: developer에만 사용.
- role preamble(역할 정체성, commit/push 등 git 비가역·board 조작·어댑터 `.claude/.codex/.opencode` 수정 금지)은 엔진이 자동 합성한다. 프롬프트 파일에는 작업만 담고 금지 문구를 중복하지 않는다.
- `--ticket`이 있는 developer·code-reviewer·architect 실 실행은 성장 사본을 자동 준비하고 그 절대경로와 자기 역할 절만 편집하라는 제한을 role preamble에 더한다. Codex named permission profile은 더 강한 기존 격리를 보존한다. Claude·OpenCode처럼 단일 경로 쓰기 격리를 보장하지 못해도 경고 후 사용자가 고른 target으로 계속 실행하며, 역할 규약과 위임 전후 git/touches 감사가 범위 밖 변경을 loud하게 표면화한다. target 자동 대체·새 reviewer opt-in·새 sandbox는 추가하지 않고 ticket 없는 reviewer의 종전 read 계약도 유지한다.
- 병렬 cross wave는 OpenCode가 제공하는 호출측 동시 실행으로 동기·stateless `pm_delegate` 호출을 병렬화한다.
- 결과: `rc=0` 성공(stdout 첫 줄=실행 provenance, 폴백 시 실제 하네스 포함; 이후 최종 reply; raw 파일 박제), `rc=1` 실패(loud·raw 경로 stderr), `rc=3` opt-in OFF. PM이 reply를 검토하고 board를 갱신하며 위임 대상은 board를 조작하지 않는다.
- `--ticket T-NNNN`은 해당 ticket `touches`를 허용 집합으로 전후 워크스페이스를 비교해 범위 밖 신규/변경/커밋을 stderr 경고한다(차단 아님·rc 불변). 생략 시 허용 0이라 모든 변경을 경고한다. **dev 위임에는 `--ticket`이 표준**.
- secret scan이 막으면 전 탐지 목록(발췌·판정·축), 승인 토큰, `--secret-scan-ack <digest>` 재실행 커맨드를 출력한다. **PM(LLM)이 반사적으로 재실행하지 않는다.** 모든 발췌를 읽고 시크릿을 논하는 텍스트(오탐)인지 실 크리덴셜(정탐)인지 판단한다. 조금이라도 모호하면 발췌를 사용자에게 제시하고 승인받은 뒤에만 ack한다. 정탐이면 ack 금지, 해당 내용을 제거해 프롬프트를 재작성한다. 승인은 프롬프트 전문+해소 수신자(harness:model)에 결속된 건별 1회이며 1자나 수신자 변경 시 재승인. ack로 통과한 실행은 **폴백이 억제**되므로(위 §매핑 조회 참조) 인프라 실패가 폴백 없이 `rc=1`로 끝난다 — 그 조합을 기대하지 말고 수신자를 명시해 재실행한다.

### opt-in 게이트

cross 위임은 코드/프롬프트·worktree 내용을 외부 하네스로 전송한다. 기본 OFF인 `delegate_enabled`가 꺼지면 스폰 없이 `rc=3`:

```ini
# local.conf (per-clone·git-ignored)
delegate_enabled = true
```

`=true`는 worktree 내용·정제된 환경의 외부 송신과 과금을 사용자가 수용하는 계약. same-harness native는 외부 송신이 없어 게이트 밖이다.

## 실행 패턴

### developer 위임

```
task tool 호출:
  description: "T-NNNN implement"
  subagent_type: developer
  prompt:
    "T-NNNN 을 구현하라.

     성장 티켓 사본(절대경로): <prepare JSON의 copy>. 이 파일의 최신
     `pm-ticket-section:start/end role=developer` 내부만 채우고 capability는 요구·기록하지 마라.

     세션명: orch-dev-TNNNN (board.py 조작은 orchestrator(PM) 담당·dev 는 코드+테스트만).
     작업 위치(worktree 절대경로): <해소 절대경로 — task-mode 시·슬롯/솔로는 생략>.

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
     - DoD 각 항목별 충족 evidence 명시"
```

> ⚠ **kill 되어도 산출은 남는다 — 단 `pm_delegate`/`external_review` 실행에 한한다.**
> **cross-harness** 위임(`pm_delegate.py`)과 추가 리뷰(`external_review.py`)는 raw 를 실행 *전*에
> 공유 JSON 장부(`.project_manager/.local/raw_outputs.json`)에 등재하고 종료 시 감사 관측치
> (`rc`·`elapsed_sec`·`silence_sec`)로 마감한다. 백그라운드 호출이 끊겨 stdout(그 안의 raw 경로)을
> 잃어도 `python3 .project_manager/tools/pm_delegate.py raw [--unfinished]` 로 절대경로를 조회하라 —
> **미마감 레코드 자체가 kill 증거**다. 재위임 전에 반드시 확인한다(완성분을 버리고 중복 과금하는 경로).
> **native 위임은 raw 장부에는 남지 않지만 성장 사본에는 남는다** — 같은 하네스 안에서 도는 위임은
> 위 native prepare→spawn→harvest를 수행한다. 하네스 보고/전사가 끊겨도 prepare가 출력한 사본을
> 먼저 읽고, `ticket harvest --copy ... --cwd ... --capability-stdin`에 PM이 보관한 capability를
> stdin으로 공급해 회수한 뒤에만 재위임한다.
> **어느 장부를 봤는지 첫 줄로 확인한다** — `조회 장부: <절대경로>`. 장부는 **엔진 사본별**이라
> `경고: 다른 엔진 사본 장부가 있습니다(이 조회에서는 읽지 않음): <경로>` 가 뜨면 자동 대체 조회가
> 된 게 아니다 — 표시된 사본에서 **명시적으로 다시 조회**하라. `--output-dir DIR` 로 저장한 산출은
> `pm_delegate.py raw --output-dir DIR` 로 조회한다.

### code-reviewer 위임

```
task tool 호출:
  description: "T-NNNN review"
  subagent_type: code-reviewer
  prompt:
    "T-NNNN 의 변경을 검토하라.

     성장 티켓 사본(절대경로): <prepare JSON의 copy>. 코드·board·git은 수정하지 말고, OpenCode
     reviewer의 허용된 Bash write로 이 파일의 최신 `pm-ticket-section:start/end role=code-reviewer`
     내부에만 판정 근거를 기록하라. edit/write 도구 금지는 코드와 다른 파일에 그대로 적용된다.

     변경 파일: <touches 인자 그대로 인용>.
     작업 위치(병렬 wave 시 격리 스냅샷): <아래 §게이트 격리 스냅샷으로 만든 gate worktree
     절대경로>. 그 격리 스냅샷에서만 읽고 검토하라 — **공유 트리(dev 라이브 편집 중) 및 그 안에서의
     git 조작 금지**(checkout/stash/reset 등이 병렬 dev 의 WIP 를 덮는다). 솔로(비병렬)면 이 줄 생략.

     ⚠️ status.md / log/current.md 갱신은 orchestrator(PM) 담당 — 그 누락은 developer
     must-fix 아님.
     소환된 domain 페이지가 있으면 그 wiki DoD(touch∩covers 갱신 soft step) 반영 여부도 점검.

     회귀 범위: <티켓 테스트 파일 목록> 만. **전체 회귀를 돌리지 마라**(전량 검증은 릴리즈 절차
     1단계 1회 — PM 담당). 범위 밖 실패를 이유로 반려하지 마라.
     (2라운드 이후) 확인 전용 판정 선행: 직전 must-fix 를 MF-n 별 해소/미해소/퇴행으로 먼저
     판정하라 — probe 재실행 실측값 포함. 신규 발견은 그 뒤에 NEW 라벨로 분리해 보고하라.

     완료 시 보고:
     - (2라운드 이후) MF-n 별 해소/미해소/퇴행 + probe 실측값
     - must-fix (수정 필수·프로젝트 고유 제약 위반·결함 — probe 를 함께 명시하라: 무엇을 돌리면
       미해소가 드러나는가)
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시"
```

reviewer task 직전과 종료 직후 `git status --short`·`git diff --name-only`를 같은 worktree에서
대조한다. 역할 밖 변경이 있으면 회수 범위를 넓히지 말고 loud하게 보고한다. 단일 경로 쓰기 격리를
강제하지 못한다는 warning은 이 감사를 생략하거나 선택 target을 바꾸는 근거가 아니다.

### architect 위임·재설계

```
task tool 호출:
  description: "T-NNNN design"
  subagent_type: architect
  prompt:
    "T-NNNN 의 설계 또는 재설계를 수행하라.

     성장 티켓 사본(절대경로): <prepare JSON의 copy>. 이 파일의 최신
     `pm-ticket-section:start/end role=architect` 내부에만 경계 실측·불변식·표면 상한·테스트 전략을
     기록하라. 재투입이면 이전 설계·developer·code-reviewer 절을 대조하고, 새로 준비된 최신 architect
     절을 직접 성장시켜 결함과 변경 결정을 남겨라. capability는 요구·기록하지 마라."
```

architect도 위 native `ticket prepare` 뒤 `task`를 호출하고 종료 뒤 `ticket harvest
--capability-stdin`을 실행한다. 재설계는 새 prepare가 지정한 최신 ordinal을 성장시키며 harvest의
stale·ticket·role·ordinal·HMAC 검증을 그대로 통과해야 한다.

> **fix 라운드 프롬프트는 리뷰어 보고서 원문으로 만든다** — must-fix 원문을 그대로 싣고 PM 판단
> (기각·처분 재정의)만 몇 줄 덧붙인다. PM 재작성은 전달 손실+토큰 낭비다. cross fix 라운드는
> `pm_delegate --resume-from <T-NNNN>` 으로 **직전 dev 세션을 재사용**한다(cold 재투입은 티켓+코드
> 재섭취를 라운드마다 다시 낸다 — fresh 는 resume 미일치 폴백·전사 과대 시에만). 같은 MF 가 2라운드
> 연속 미해소면 라운드 추가가 아니라 재설계·분할로 전환한다(내부 라운드 상한 3 — `pm_playbook.md`
> §"라운드 프로토콜").

reviewer와 **병행해 추가 리뷰어(additional reviewer) 교차검증**을 실행한다:
`python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
ADR 본문 정합 필요 시 `--paths`에 **코드 경로+ADR을 함께 나열**한다. `--paths`는 `--ticket` touches를 대체한다. 전제: `additional_reviewer_enabled=true`. 상세: `pm_playbook.md` §"검토 루프".
