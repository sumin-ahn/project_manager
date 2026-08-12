---
name: pm-dev-delegate
description: "orchestrator dev/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check + background 옵션. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
audience: pm-internal
---

# /pm-dev-delegate T-NNNN [--role developer|code-reviewer] [--background] — orchestrator 위임

Agent 툴 + `subagent_type: developer|code-reviewer` + `run_in_background` 옵션. ticket 본문이 self-contained 의무 충족 시 위임 프롬프트는 한 줄.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

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

## cross-harness 판정과 위임 (`pm_delegate`)

위임 전 target이 PM과 같은 하네스(native)인지 다른 하네스(cross)인지 판정한다. 1차 판정은 이 카드이며 `pm_delegate.py` same-harness 경고는 never-block 백스톱. 매핑은 `local.conf`의 `delegate.<role>[.<tier>]`.

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

- **target harness == PM 하네스**: 아래 실행 패턴대로 native 위임(claude=Agent 툴 `subagent_type`, codex=`spawn_agent`, opencode=subagent). `pm_delegate`를 호출하지 않는다.
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
- 병렬 wave는 호출측 PM 하네스의 background 실행(claude Bash `run_in_background` 등)으로 동기·stateless `pm_delegate` 호출을 병렬화한다.
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
Agent 툴 호출:
  description: "T-NNNN implement"
  subagent_type: developer
  run_in_background: true (병렬 wave 시) | false (직렬·이 결과에 의존 시)
  prompt:
    "T-NNNN 을 구현하라.

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
> **native 위임은 장부에 남지 않는다** — 같은 하네스 안에서 도는 위임(각 하네스의 native
> 서브에이전트 경로)은 `pm_delegate` 를 경유하지 않으므로 이 조회 대상이 아니다. native 산출은
> 하네스 자체의 보고/전사에서 찾는다.
> **어느 장부를 봤는지 첫 줄로 확인한다** — `조회 장부: <절대경로>`. 장부는 **엔진 사본별**이라
> `경고: 다른 엔진 사본 장부가 있습니다(이 조회에서는 읽지 않음): <경로>` 가 뜨면 자동 대체 조회가
> 된 게 아니다 — 표시된 사본에서 **명시적으로 다시 조회**하라. `--output-dir DIR` 로 저장한 산출은
> `pm_delegate.py raw --output-dir DIR` 로 조회한다.

### code-reviewer 위임

```
Agent 툴 호출:
  description: "T-NNNN review"
  subagent_type: code-reviewer
  run_in_background: true (병렬 reviewer 시) | false (단일 reviewer 시)
  prompt:
    "T-NNNN 의 변경을 검토하라.

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

> **fix 라운드 프롬프트는 리뷰어 보고서 원문으로 만든다** — must-fix 원문을 그대로 싣고 PM 판단
> (기각·처분 재정의)만 몇 줄 덧붙인다. PM 재작성은 전달 손실+토큰 낭비다. cross fix 라운드는
> `pm_delegate --resume-from <T-NNNN>` 으로 **직전 dev 세션을 재사용**한다(cold 재투입은 티켓+코드
> 재섭취를 라운드마다 다시 낸다 — fresh 는 resume 미일치 폴백·전사 과대 시에만). 같은 MF 가 2라운드
> 연속 미해소면 라운드 추가가 아니라 재설계·분할로 전환한다(내부 라운드 상한 3 — `pm_playbook.md`
> §"라운드 프로토콜").

reviewer와 **병행해 추가 리뷰어(additional reviewer) 교차검증**을 실행한다:
`python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
ADR 본문 정합 필요 시 `--paths`에 **코드 경로+ADR을 함께 나열**한다. `--paths`는 `--ticket` touches를 대체한다. 전제: `additional_reviewer_enabled=true`. 상세: `pm_playbook.md` §"검토 루프".

#### 게이트 격리 스냅샷 (병렬 wave · 내부 reviewer 전용)

병렬 dev가 공유 트리를 라이브 편집 중이면 PM은 reviewer 위임 전에 검토 대상의 **staged** 상태를 격리 worktree로 스냅샷해 dev 편집·reviewer git 조작(sensitivity `git checkout` 등)의 경합을 막는다. 스냅샷 생성·신선도 검증은 엔진 도구 `gate_snapshot.py`가 수행한다 — 검토 대상 경로의 staged 내용이 working tree와 다르면(미-stage dev 산출 = stale index) 생성을 fail-loud로 거부해 stale 검토(false-green)를 기계로 차단한다. `<scratch>`는 repo 밖 경로(`/tmp` 또는 repo 상위 `..`), 최종 경로는 `<scratch>/gate-<T>`. 출력 경로는 공유 worktree·같은 저장소 git 공용 디렉터리·다른 등록 worktree 안이면 거부된다(prunable 등록 재사용은 `git worktree prune` 처방).

1. **stage** — 검토 대상 dev 산출을 먼저 `git add <경로>` 한다. **다라운드 게이트는 라운드마다 다시** — 누락하면 도구가 불일치로 차단한다(그게 이 도구가 닫는 클래스다).
2. **생성** —

   ```bash
   python3 .project_manager/tools/gate_snapshot.py \
       --repo <공유 트리 절대경로> --output <scratch>/gate-<T> \
       --paths <검토 파일1> <검토 파일2> ...
   ```

   - **병렬 wave에서는 `--paths`를 파일 단위로** 지정한다 — 디렉터리를 주면 같은 디렉터리의 타 dev WIP(tracked-unstaged·untracked)가 불일치로 검출돼 차단된다(설계 동작). 진단 안내대로 검토 대상이면 `git add`, 타 dev WIP면 파일 단위로 좁힌다.
   - rc=0 = 스냅샷이 캡처 시점 index와 일치함을 도구가 검증(HEAD OID·index 엔트리·파일 집합 이중 bookend·submodule gitlink 원본 불변·eol 정규화 비교 포함). rc=1 = fail-loud — 자동 `git add` 해소는 하지 않는다(타 dev WIP 오염 금지).
3. **주입** — reviewer 프롬프트 작업 위치에 `<scratch>/gate-<T>` **절대경로**를 넣고 그 스냅샷에서만 읽고 검토시킨다. 그 안의 git 조작(checkout·stash 등)은 공유 트리에 닿지 않는다.
4. **제거** — 리뷰 후:

   ```bash
   git worktree remove --force <scratch>/gate-<T>
   ```

   `--force`는 오버레이가 미커밋이라 dirty인, 버려도 안전한 스냅샷 제거에 필요.

- 내부 reviewer만 대상. codex `external_review`는 **staged diff** 기반이라 이미 스냅샷-안정 → 격리 **대상 아님**(라이브 working tree 를 읽지 않는다). 단 staged 가 최신인지는 같은 원칙이다 — 검토 파일을 재-`git add` 한 뒤 실행한다.
- 솔로(비병렬)는 격리 선택.
- 격리와 프롬프트의 *공유 트리 git 조작 금지* 완화는 **병행**한다(**이중 방어** — 절차가 경합을 구조적으로 막고, 프롬프트가 사고성 git 조작을 막는다).

## 병렬 wave touches cross-check

dev N 동시 spawn 전:

- 모든 claimed ticket touches가 완전 disjoint(file 겹침 0)인지 확인. 공통 통합 파일의 함수 단위 추가는 완화 조건으로 허용.
- 같은 함수·같은 줄 동시 수정은 차단.
- baseline 회귀는 dev cycle 후 한 번에 측정(race 회피).

## reviewer 후 처리

- **PM 직접 fix**: 1줄·1패턴·dev가 작업하지 않는 영역.
- **dev 재작업**: 여러 줄 또는 dev가 같은 file 작업 중.
- **별도 ticket 후보 메모**: 본 ticket 범위 밖/후속 caller.
- **suggestion 보류**: 운영 영향 0·기능 충분.

reviewer 결과를 그대로 믿지 말고 should-fix 전 코드 흐름을 PM이 독립 점검한다. 부정확하면 변경하지 않고 `log/current.md`에 영구 기록. 다른 ticket 결함을 현재 ticket 영역으로 잘못 attribute할 수 있으므로 실제 영역 확인 후 분기한다.

## 운용

- board 조작은 PM, 서브에이전트는 구현/검토만.
- dev 보고에는 변경 파일, 신규 테스트, 지정 회귀(범위 명시), DoD별 evidence를 강제. 전체 회귀는 릴리즈 절차 1단계 1회(PM)다.
- background 우선. 다음 ticket이 결과에 의존하면 foreground.
- 프롬프트가 길어지면 ticket 본문을 보강한다.
- 같은 dev를 반복 resume하면 transcript 누적으로 컨텍스트 한도에 실패한다(14회 resume에서 "Prompt is too long"). 대략 5~6회↑면 새 에이전트에 자족 프롬프트로 재투입하고 현 코드 상태(신설 심볼·미커밋 변경)를 요약한다. 산출물은 워킹트리에 유지된다.

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기
- `.project_manager/tools/pm_delegate.py` — cross-harness 위임
- `.claude/agents/developer.md`
- `.claude/agents/code-reviewer.md`
