---
name: pm-dev-delegate
description: "Codex native spawn_agent 기반 dev/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
audience: pm-internal
---

# /pm-dev-delegate T-NNNN [--role developer|code-reviewer] — orchestrator 위임

> {{PROJECT_NAME}} PM 의 Codex native `spawn_agent` 위임 표준 프롬프트. 역할은
> `agent_type="developer|code-reviewer"`로 고르고, spawn 이 반환한 thread 는 비동기로 진행된다.
> ticket 본문이 self-contained 의무를 충족하면 위임 프롬프트는 한 줄이다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사전 조건

- ticket 이미 claim (`pm-wave-claim` 통과 · 세션 정체성 canonical `<repo>_<N>` · 솔로(M=1)는 생략).
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
    --prompt-file <프롬프트 파일 절대경로> --cwd <작업 worktree 절대경로> [--tier normal|hard]
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

     세션명: orch-dev-TNNNN (board.py 조작은 orchestrator(PM) 담당·dev 는 코드+테스트만).
     작업 위치(worktree 절대경로): <F6 해소 절대경로 — task-mode 시·슬롯/솔로는 생략>.

     ticket 본문은 python3 .project_manager/tools/board.py show T-NNNN 로 확인.
     본문이 self-contained — 목표/인터페이스/결정/DoD/참고 절 대로 구현.
     (PM 첨부 — 소환된 domain 페이지: <domain affected 출력 경로·있으면>. ⚠ 표시분은 stale 이니 맹신 말 것.)

     완료 시 보고:
     - 변경 파일 목록
     - 신규 테스트 수
     - 전체 회귀 결과 (A / B passed)
     - DoD 각 항목별 충족 evidence 명시""",
)
```

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

     변경 파일: <touches 인자 그대로 인용>.
     작업 위치(병렬 wave 시 격리 스냅샷): <아래 §게이트 격리 스냅샷으로 만든 gate worktree
     절대경로>. 그 격리 스냅샷에서만 읽고 검토하라 — **공유 트리(dev 라이브 편집 중) 및 그 안에서의
     git 조작 금지**(checkout/stash/reset 등이 병렬 dev 의 WIP 를 덮는다). 솔로(비병렬)면 이 줄 생략.

     ⚠️ status.md / log/current.md 갱신은 orchestrator(PM) 담당 — 그 누락은 developer
     must-fix 아님.
     소환된 domain 페이지가 있으면 그 wiki DoD(touch∩covers 갱신·T-0081 soft step) 반영 여부도 점검.

     완료 시 보고:
     - must-fix (수정 필수·프로젝트 고유 제약 위반·결함)
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시""",
)
```

> ⚙️ reviewer 위임과 **병행해 codex 외부 교차검증**을 돌린다 (표준 리뷰 게이트):
> `python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`
> (ADR 본문 정합 필요 시 `--paths` 에 **코드 경로+ADR 함께 나열** — `--paths` 는
> `--ticket` touches 를 *대체*함). 전제
> `external_review_enabled=true`. 상세는 `pm_playbook.md` §"검토 루프".

#### 게이트 격리 스냅샷 (병렬 wave · 내부 reviewer 전용)

병렬 wave(dev 가 공유 트리를 **라이브 편집 중**)에서 **내부 reviewer** 를 위임할 땐, 위임 *전* PM
이 리뷰 대상(= 미리 `git add` 한 **staged** 상태)을 격리 worktree 로 스냅샷한다 — 리뷰가 읽는 트리
가 dev 편집·리뷰 자신의 git 조작(sensitivity `git checkout` 등)으로 흔들리지 않게 **절차 자체가
경합 불가능**해진다(2회 실측: T-0389 리뷰 false-red · T-0402 리뷰 ↔ T-0409 dev 편집 실경합).

`<scratch>` = **repo 트리 밖** 경로(예: OS 임시 디렉토리 `/tmp`, 또는 repo 상위 `..` — 최종 경로는
`<scratch>/gate-<T>`).

1. **생성** — staged(index)만 격리 스냅샷(unstaged 병렬 WIP 는 자동 제외). ⚠ **두 커맨드 모두
   공유(메인) 트리 cwd 에서 실행 — gate 디렉토리로 `cd` 후 실행 금지**: `checkout-index` 는 cwd 가
   해소하는 worktree 의 index 를 읽으므로, gate 안에서 돌리면 staged 가 빠진 HEAD-only 스냅샷이
   조용히 만들어져 리뷰가 옛 코드를 통과시킨다(false-green).
   ```bash
   git worktree add --detach <scratch>/gate-<T>
   git checkout-index -a -f --prefix=<scratch>/gate-<T>/
   ```
2. **주입** — reviewer 위임 프롬프트의 «작업 위치» 에 `<scratch>/gate-<T>` **절대경로**를 박아
   넣는다(위 code-reviewer 프롬프트 참조). reviewer 는 그 격리 스냅샷에서만 읽고 검토하며, 그 안의
   git 조작(checkout·stash 등)도 공유 트리에 닿지 않는다.
3. **제거** — 리뷰 종료 후:
   ```bash
   git worktree remove --force <scratch>/gate-<T>
   ```
   (`--force` = 오버레이가 미커밋이라 스냅샷 worktree 가 dirty — 버려도 안전한 스냅샷이라 강제 제거.)

- **대상 = 내부 reviewer 뿐.** codex `external_review` 는 **staged diff** 기반이라 이미 스냅샷-안정
  → 격리 **대상 아님**(라이브 working tree 를 읽지 않는다).
- **솔로(비병렬) 리뷰는 격리 선택** — 경합할 병렬 dev 가 없으면 종전대로 공유 트리에서 검토해도 된다.
- 이 격리와 프롬프트의 *공유 트리 git 조작 금지* 완화는 **병행**한다(**이중 방어** — 절차가 경합을
  구조적으로 막고, 프롬프트가 사고성 git 조작을 막는다).

## touches disjoint 안전성 cross-check (병렬 wave)

병렬 wave (dev N 동시 spawn) 시 PM 이 위임 전 검증:

- 모든 claimed ticket 의 touches 가 *완전 disjoint* (file 겹침 0)? — *공통 통합 파일 함수 단위 추가* 는 완화 조건으로 OK.
- 같은 함수·같은 줄 동시 수정은 차단.
- baseline 회귀 측정은 *dev cycle 끝난 후 한 번에* (race 회피).

## must-fix 분기 (reviewer 후)

reviewer 보고 후 PM 처리:

- **PM 직접 fix** — 1줄·1패턴·dev 안 도는 영역. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 dev 가 같은 file 작업 중.
- **별도 ticket 후보 메모** — 본 ticket 범위 외 / 후속 caller 추가 시.
- **suggestion 보류** — 운영 영향 0·기능 충분.

## reviewer 분석 cross-check

reviewer 도 항상 옳지 않다. PM 가 should-fix 처리 전 *코드 흐름 자체* 독립
점검·부정확이면 변경 불필요 + log/current.md 영구 기록. 특히 *reviewer 영역 attribute
부정확* — reviewer 가 *다른 ticket 영역의 결함을 현재 ticket 영역으로 잘못
attribute* 가능. PM 이 진짜 영역 확인 후 fix 분기 결정.

## 결정

- **board.py 조작은 orchestrator(PM)** — 위임 프롬프트에 명시. 서브에이전트는 구현/검토만.
- **dev 자기 보고 표준 형식 강제** — 위임 프롬프트에 *DoD 각 항목별 충족 evidence* 명시 요구.
- **background 우선** — 병렬 wave 효율 ↑. 단 검토 결과에 다음 ticket 의존 시 foreground.
- **위임 프롬프트는 한 줄** — ticket 본문이 self-contained 의무 → 추가 컨텍스트 불필요. 길어지면 ticket 본문 보강.
- **해소 절대경로 주입** — task-mode dev 위임은 F6 로 해소한 worktree 절대경로 실값을 프롬프트에 명시(짐작 제거·cwd 비참여·T-0355). ⑰ 카드 생성화(T-0362) 전이라 현 wave 는 프롬프트 명시 주입까지.
- **native 단락 판정 = 이 카드** — target 하네스 == 내 하네스(codex)면 `spawn_agent` 네이티브 위임(외부 송신 0),
  cross 면 `pm_delegate.py` 채널(외부 송신·opt-in 필요). `pm_delegate` same-harness 경고는 백스톱(never-block).

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기 단일 진실
- `.project_manager/tools/pm_delegate.py` — cross-harness 위임 채널 엔진(매핑 해소·argv·role preamble·opt-in 게이트·ADR-0075)
- `.claude/agents/developer.md` — developer 서브에이전트 정의
- `.claude/agents/code-reviewer.md` — code-reviewer 서브에이전트 정의
