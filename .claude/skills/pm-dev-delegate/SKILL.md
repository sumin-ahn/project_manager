---
name: pm-dev-delegate
description: "orchestrator dev/code-reviewer 위임 표준 프롬프트 + touches disjoint 안전성 cross-check + background 옵션. claim 은 별도 (pm-wave-claim). reviewer 위임 시 status.md/log/current.md 갱신 책임 명시. Triggers: 'dev 위임', 'reviewer 위임', 'T-NNNN 위임', 'pm-dev-delegate'."
audience: pm-internal
---

# /pm-dev-delegate T-NNNN [--role developer|code-reviewer] [--background] — orchestrator 위임

> {{PROJECT_NAME}} PM 의 orchestrator 위임 표준 프롬프트. Agent 툴 +
> `subagent_type: developer|code-reviewer` + `run_in_background` 옵션. ticket
> 본문이 self-contained 의무 충족 시 위임 프롬프트는 한 줄.

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
python3 .project_manager/tools/board.py regression run --task <이름> [--repo <X> [--slot <N>]]
# 출력: "regression: 작업공간(task <이름>) → <worktree 절대경로>"
```

- 그 `<worktree 절대경로>`를 developer 위임 프롬프트의 **작업 위치**로 박아 넣는다(짐작 제거).
- task 가 슬롯을 2개↑ 보유해 모호하면 F6 이 **에러**(⑦·암묵 선택 금지) — `--repo`/`--slot` 로 특정 후 주입.
- 슬롯 세션(비-task)·솔로(M=1)는 종전대로 — 이 주입은 task-mode 에서만.

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
     작업 위치(worktree 절대경로): <F6 해소 절대경로 — task-mode 시·슬롯/솔로는 생략>.

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
     소환된 domain 페이지가 있으면 그 wiki DoD(touch∩covers 갱신·T-0081 soft step) 반영 여부도 점검.

     완료 시 보고:
     - must-fix (수정 필수·프로젝트 고유 제약 위반·결함)
     - should-fix (권장·운영 영향 있음)
     - suggestion (개선 옵션·운영 영향 없음)
     - 통과/반려 명시"
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

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기 단일 진실
- `.claude/agents/developer.md` — developer 서브에이전트 정의
- `.claude/agents/code-reviewer.md` — code-reviewer 서브에이전트 정의
