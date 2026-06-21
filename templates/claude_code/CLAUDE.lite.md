# CLAUDE.md — lite 진입 (경량·자족)

> Claude Code 세션 진입점 **lite 판**. 이 한 파일 + `board.py` + `.claude/skills/` 만으로
> PM happy-path(부트스트랩 → ticket 발행 → 위임 → finish)를 자족 운영하도록 압축했다.
> **무거운 방법론 문서(`pm_role.md`·`pm_playbook.md`)는 auto-load 하지 않는다** — happy-path
> 밖(복잡 운영)일 때만 lazy Read(아래 §막혔을 때·끝 §lazy). 회사 200K / 경량 배포용. (2D 무게축 lite-A)

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 1. 부트스트랩 (세션 시작)

1. **이 파일** — 이미 로드됨.
2. **보드** — `{{PY}} .project_manager/tools/board.py list` (지금 잡을 수 있는 ticket).
3. **아키텍처** — [`architecture.md`](.project_manager/wiki/architecture.md) (현재-아키텍처 단일 진실 · ① live / ② target · ADR-0022 · 부트스트랩 1순위 · 충돌 시 이게 기준).
4. **상태** — [`.project_manager/wiki/status.md`](.project_manager/wiki/status.md) (모듈 진행상태·비고 · judgment-only) + [`pm_state.md`](.project_manager/wiki/pm_state.md) (세션 window·남은 작업, per-clone).
5. **직전 핸드오프** — `{{PY}} .project_manager/tools/pm_log.py tail` (마지막 entry 만).

> 세션명: `claim` 의 `--session <name>` 인자로 전달(우선순위 `--session` > `$PM_SESSION_NAME`[구 `$CLAUDE_SESSION_NAME` alias] > `local.conf session=` > `hostname-pid`).
> 첫 turn 권장 보고: board 1줄 + 직전 세션 요약 3~5줄 + 다음 옵션 + 결정 요청(*무엇부터?*).

## 2. 작업 원칙 (반드시)

- **작은 단위 분할 → 단계별 테스트 검증.** 한 모듈 = 한 ticket = 한 단계.
- **테스트 없이는 끝난 게 아니다.** 회귀 `{{TEST_CMD}}` 통과가 완료 조건.
- **최소 변경.** ticket 이 요구한 것만. 무관한 리포맷·기능 추가 금지.
- **약어보다 명시적 풀네임.**

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 아키텍처 불변식·안전 경계(서브에이전트·전 세션 상속). 없으면 이 절 삭제. -->

## 3. ticket 발행 계약 (PM 자족 — board.py new)

새 작업은 ticket 으로 발행한다. **본문이 단일 진실 — 그것만 보고 구현 가능해야 한다.**

```bash
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --depends T-0001 --tag phase-1
# → open/ 에 T-NNNN(또는 영역 prefix 시 T-PAY-NNN) 스켈레톤 생성. 본문을 아래 형식으로 채운다.
```

본문 필수 절 (self-containment — `board.py lint` 가 thin·dangling 검사):

- **목표** — 무엇을·왜 (1~3줄).
- **인터페이스** — 함수/CLI 시그니처·입출력·파일 경로.
- **결정** — 구현 분기에서 택한 방향·근거(있으면).
- **완료 조건 (DoD)** — 체크 가능한 항목(테스트 green·산출 파일·판정/비고 갱신).
- **참고** — 설계 근거 링크(`[[wikilink]]` 는 실재 파일이어야). `depends_on`/`blocks` 는 frontmatter.

> 발행·분할·depends_on 변경은 PM 자율(사후 log). 채번(T-NNNN)·area prefix·thin 기준 세부는
> 필요 시 [`pm_playbook.md`](.project_manager/wiki/pm_playbook.md) Read.

## 4. 위임 (구현은 서브에이전트에)

PM 은 코드를 직접 안 짠다 — `claim → 위임(dev) → 검토(reviewer) → finish`.

- **dev**: `Agent`(developer) 에 위임. 프롬프트 = "T-NNNN 구현. 본문이 단일 진실(`board.py show T-NNNN`). board/status/log 는 PM 담당 — 너는 코드+테스트만."
- **reviewer**: `Agent`(code-reviewer) 로 독립 검토(generate≠evaluate). must-fix/should-fix/통과·반려.
- **병렬 위임은 touches disjoint 일 때만.** reviewer 도 틀릴 수 있다 — should-fix 는 PM 이 흐름 cross-check 후 적용.
- skill 이 있으면 표준 프롬프트 사용: `/pm-wave-claim`·`/pm-dev-delegate`·`/pm-wave-finish`.

## 5. 완료 부기 (PM 손)

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
```

추가로: `status.md` 모듈 행 갱신 · `log/current.md` entry append · 회귀 `{{TEST_CMD}}` green 확인 ·
**git commit**(논리적 체크포인트·메시지 말미 `Co-Authored-By: Claude` 트레일러). skill `/pm-wave-finish` 가 스칼라·skeleton·board·stage 를 자동화한다.

## 6. 자주 쓰는 명령

```bash
{{TEST_CMD}}                                                  # 전체 테스트(수의 단일 진실 = status.md)
{{PY}} .project_manager/tools/board.py list|show|claim|complete|new|lint
{{PY}} .project_manager/tools/pm_log.py tail                  # 마지막 entry
{{PY}} .project_manager/tools/pm_bootstrap.py                 # board·git·log 일괄 dump(선택)
{{PY}} .project_manager/tools/pm_handoff.py --dry-run         # 세션 종료 핸드오프
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run   # 엔진 동기화(메인테이너)
```

PM workflow skill 카탈로그(`/pm-bootstrap`·`/pm-handoff`·`/pm-wave-claim`·`/pm-dev-delegate`·
`/pm-wave-finish`·`/pm-regression`·`/spike-new`)의 용법·backbone 단일 진실은
[`.claude/skills/`](.claude/skills/) 각 SKILL.md.

> **Windows/인코딩**: 엔진이 인코딩을 코드로 처리(PM 7차)하므로 env 없이 동작 — Windows/CP949·PowerShell 서도 한글 깨짐 0.
> 드물게 필요하면 셸별 문법(PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`). `{{PY}}` 는 `board.py init` 이 PATH 탐지(Windows=`python`)로 채운다.

## 7. 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py·ticket_finish.py·pm_*.py (숨김 — `ls -a`) |
| `.project_manager/wiki/` | 산출물 (status·pm_state / pm_role·pm_playbook(lazy) / log / decisions / raw) |
| `.claude/skills/` · `.claude/agents/` | PM workflow skill · 서브에이전트(architect·developer·code-reviewer) |

## 막혔을 때 / lazy 참조 (happy-path 밖 → 그때 Read)

- 의존 미완·외부 키 없음 → `board.py block --reason "..."` · 잘못 claim → `board.py unclaim`.
- **복잡 운영** — wave 충돌·incident 처리·핸드오프 심층·멀티-PM clone·결정권한 경계(사용자 게이트/금지)·
  프레임워크 갱신 절차 → [`.project_manager/wiki/pm_role.md`](.project_manager/wiki/pm_role.md)(정적 운영 매뉴얼) ·
  [`.project_manager/wiki/pm_playbook.md`](.project_manager/wiki/pm_playbook.md)(Wave 패턴·메타 정책) Read.
- 모르는 구조 결정 → ADR 작성 후 진행([`.project_manager/wiki/decisions/`](.project_manager/wiki/decisions/)).
