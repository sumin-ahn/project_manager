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

- ticket 이미 claim (`pm-wave-claim` 통과 · 세션 정체성 canonical `<repo>_<N>` · 솔로(M=1)는 생략).
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
- 역할 매핑 미설정은 `rc=1` fail-loud. `local.conf`에 `delegate.<role>.harness/.model`을 채운다. 조용한 폴백은 없다. 단 `delegate.<role>[.<tier>].fallback.harness/.model[/.reasoning]`을 명시하면 **인프라 실패**(스폰 실패·한도·타임아웃·stall)에만 1단 폴백하고 사유를 stderr에 표기한다. 판정 반려/denylist 차단에는 발동하지 않는다. 권장 조합은 claude/opus.
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
- secret scan이 막으면 전 탐지 목록(발췌·판정·축), 승인 토큰, `--secret-scan-ack <digest>` 재실행 커맨드를 출력한다. **PM(LLM)이 반사적으로 재실행하지 않는다.** 모든 발췌를 읽고 시크릿을 논하는 텍스트(오탐)인지 실 크리덴셜(정탐)인지 판단한다. 조금이라도 모호하면 발췌를 사용자에게 제시하고 승인받은 뒤에만 ack한다. 정탐이면 ack 금지, 해당 내용을 제거해 프롬프트를 재작성한다. 승인은 프롬프트 전문+해소 수신자(harness:model)에 결속된 건별 1회이며 1자나 수신자 변경 시 재승인.

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
     본문이 self-contained — 목표/인터페이스/결정/DoD/참고 절 대로 구현.
     (PM 첨부 — 소환된 domain 페이지: <domain affected 출력 경로·있으면>. ⚠ 표시분은 stale 이니 맹신 말 것.)

     완료 시 보고:
     - 변경 파일 목록
     - 신규 테스트 수
     - 전체 회귀 결과 (A / B passed)
     - DoD 각 항목별 충족 evidence 명시"
```

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

     완료 시 보고:
     - must-fix (수정 필수·프로젝트 고유 제약 위반·결함)
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시"
```

reviewer와 **병행해 codex 외부 교차검증**을 실행한다:
`python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
ADR 본문 정합 필요 시 `--paths`에 **코드 경로+ADR을 함께 나열**한다. `--paths`는 `--ticket` touches를 대체한다. 전제: `external_review_enabled=true`. 상세: `pm_playbook.md` §"검토 루프".

#### 게이트 격리 스냅샷 (병렬 wave · 내부 reviewer 전용)

병렬 dev가 공유 트리를 라이브 편집 중이면 PM은 reviewer 위임 전에 미리 `git add`한 **staged** 상태만 격리 worktree로 스냅샷해 dev 편집·reviewer git 조작(sensitivity `git checkout` 등)의 경합을 막는다. `<scratch>`는 repo 밖 경로(`/tmp` 또는 repo 상위 `..`), 최종 경로는 `<scratch>/gate-<T>`.

1. **생성** — unstaged 병렬 WIP는 제외. ⚠ 두 커맨드는 모두 **공유(메인) 트리 cwd**에서 실행한다. gate로 `cd` 후 `checkout-index`를 실행하면 gate index를 읽어 staged가 빠진 HEAD-only 스냅샷으로 false-green이 난다.

   ```bash
   git worktree add --detach <scratch>/gate-<T>
   git checkout-index -a -f --prefix=<scratch>/gate-<T>/
   ```

2. **주입** — reviewer 프롬프트 작업 위치에 `<scratch>/gate-<T>` **절대경로**를 넣고 그 스냅샷에서만 읽고 검토시킨다. 그 안의 git 조작(checkout·stash 등)은 공유 트리에 닿지 않는다.
3. **제거** — 리뷰 후:

   ```bash
   git worktree remove --force <scratch>/gate-<T>
   ```

   `--force`는 오버레이가 미커밋이라 dirty인, 버려도 안전한 스냅샷 제거에 필요.

- 내부 reviewer만 대상. codex `external_review`는 **staged diff** 기반이라 이미 스냅샷-안정 → 격리 **대상 아님**(라이브 working tree 를 읽지 않는다).
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
- dev 보고에는 변경 파일, 신규 테스트, 전체 회귀, DoD별 evidence를 강제.
- background 우선. 다음 ticket이 결과에 의존하면 foreground.
- 프롬프트가 길어지면 ticket 본문을 보강한다.
- 같은 dev를 반복 resume하면 transcript 누적으로 컨텍스트 한도에 실패한다(14회 resume에서 "Prompt is too long"). 대략 5~6회↑면 새 에이전트에 자족 프롬프트로 재투입하고 현 코드 상태(신설 심볼·미커밋 변경)를 요약한다. 산출물은 워킹트리에 유지된다.

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기
- `.project_manager/tools/pm_delegate.py` — cross-harness 위임
- `.claude/agents/developer.md`
- `.claude/agents/code-reviewer.md`
