# CLAUDE.md — lite 진입 (경량·자족)

> 이 파일 + `board.py` + `.claude/skills/` 로 PM happy-path(부트스트랩 → ticket 발행 → 위임 → finish)를 운영한다. `pm_role.md`·`pm_playbook.md` 는 auto-load 하지 말고 happy-path 밖에서만 lazy Read.

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 1. 부트스트랩 (세션 시작)

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `/pm-bootstrap` dump 한 번뿐이다.

1. **이 문서(CLAUDE.md·lite 코어)** — 이미 로드된 프로젝트 규칙·형상.
2. **현재 정체성의 `pm_state`** — task는 `.project_manager/.local/tasks/<task>/pm_state.md`,
   slot은 `.project_manager/.local/slots/<repo>_<N>/pm_state.md`다(둘 다 git-ignored).
   신규 task는 bootstrap 진입 전 파일이 없어도 정상이다.
3. **`/pm-bootstrap` dump 한 번** — board·git·차수·직전 handoff 본문·남은 작업을 한꺼번에
   surface한다. Python backbone은 `{{PY}} .project_manager/tools/pm_bootstrap.py`다.
<!-- pm-bootstrap-preread:end -->

정식 계약은 `.project_manager/wiki/pm_role.md` §부트스트랩이 단일 진실이다.
`architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는 시작 시
통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다.

**현재 진실:** [`architecture.md`](.project_manager/wiki/architecture.md)는 현재 아키텍처 단일
진실이며, 옛 ADR 또는 현재 의도·실측과 충돌하면 기준으로 따른다. 바뀐 것은 읽는 시점뿐이다.

> 세션명 canonical = `<repo>_<N>`. `claim` 에 `--repo <repo> --slot <N>` 으로 전달하며 활성 lease 1개면 생략 가능. 우선순위: `--repo`/`--slot` > `$PM_SESSION_NAME`(구 `$CLAUDE_SESSION_NAME` alias) > 활성 슬롯 lease 1개면 그 세션(단일-lease 유도) > 미해소(귀속 쓰기 fail-loud). lease 행 0개면 아직 등록 전이라 `/pm-update` 1회로 홈이 첫 슬롯 행 `<repo>_1` 이 된다.
> 첫 turn 권장 보고: board 1줄 + 직전 세션 요약 3~5줄 + 다음 옵션 + 결정 요청(*무엇부터?*).

## 2. 작업 원칙 (반드시)

- **작은 단위 분할 → 단계별 테스트 검증.** 한 모듈 = 한 ticket = 한 단계.
- **테스트 없이는 끝난 게 아니다.** 회귀 `{{TEST_CMD}}` 통과가 완료 조건.
- **최소 변경.** ticket 요구만 수행하고 무관한 리포맷·기능 추가 금지.
- **약어보다 명시적 풀네임.**

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 아키텍처 불변식·안전 경계(서브에이전트·전 세션 상속). 없으면 이 절 삭제. -->

## 3. ticket 발행 계약 (PM 자족 — board.py new)

새 작업은 ticket 으로 발행한다. **본문이 단일 진실이며 그것만 보고 구현 가능해야 한다.**

```bash
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --depends T-0001 --tag phase-1
# → open/ 에 T-NNNN(또는 영역 prefix 시 T-pay-NNN) 스켈레톤 생성. 본문을 아래 형식으로 채운다.
```

본문 표준 절:

- **목표** — 무엇을·왜(1~3줄).
- **인터페이스** — 함수/CLI 시그니처·입출력·파일 경로.
- **결정** — 구현 분기에서 택한 방향·근거(있으면).
- **완료 조건 (DoD)** — 테스트 green·산출 파일·판정/비고 갱신 등 체크 가능 항목.
- **참고** — 설계 근거 링크(`[[wikilink]]` 는 실재 파일). `depends_on`/`blocks` 는 frontmatter.

`board.py lint` 는 목표·완료 조건·참고 3개만 thin 차단(`_REQUIRED_SECTIONS`)하고 dangling 을 검사하며 나머지는 권장. 발행·분할·depends_on 변경은 PM 자율(사후 log). 채번·area prefix·thin 세부는 필요 시 [`pm_playbook.md`](.project_manager/wiki/pm_playbook.md) Read.

## 4. 위임 (구현은 서브에이전트에)

PM 은 코드를 직접 짜지 않는다: `claim → 위임(dev) → 검토(reviewer) → finish`.

- **dev**: `Agent`(developer)에 위임. 프롬프트 = "T-NNNN 구현. 본문이 단일 진실(`board.py show T-NNNN`). board/status/log 는 PM 담당 — 너는 코드+테스트만."
- **reviewer**: `Agent`(code-reviewer)로 독립 검토(generate≠evaluate). must-fix/should-fix/통과·반려.
- **병렬 위임은 touches disjoint 일 때만.** reviewer 도 틀릴 수 있으므로 should-fix 는 PM 이 흐름 cross-check 후 적용.
- skill 표준 프롬프트: `/pm-wave-claim`·`/pm-dev-delegate`·`/pm-wave-finish`.

## 5. 완료 기록 (PM 손)

```bash
{{PY}} .project_manager/tools/ticket_finish.py --cluster C-<이름> [--repo <repo> --slot <N>]
```

`status.md` 모듈 행 갱신 · `log/current.md` entry append · 회귀 `{{TEST_CMD}}` green 확인 · **git commit**은 논리적 체크포인트에서 **경로 명시**:
`git commit -m "T-NNNN — <요약>" -- <ticket touches> .project_manager/wiki/status.md .project_manager/wiki/log/current.md .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md .project_manager/wiki/tickets/done/T-NNNN-<slug>.md`

bare commit 은 남이 stage 한 변경까지 싣는다. ticket 이동은 **옛·새 경로 둘 다** 지정. **신규 파일은 `git add` 선행** — 미추적 pathspec 은 `pathspec … did not match` 로 rc=1. 메시지 말미 `Co-Authored-By: Claude` 트레일러. `/pm-wave-finish` 가 스칼라·skeleton·board·stage 를 자동화한다.

## 6. 자주 쓰는 명령

```bash
{{TEST_CMD}}                                                  # 전체 테스트(수의 단일 진실 = status.md)
{{PY}} .project_manager/tools/board.py list|show|claim|complete|new|lint
{{PY}} .project_manager/tools/pm_log.py tail                  # 마지막 entry
{{PY}} .project_manager/tools/pm_bootstrap.py                 # 세션 시작 필수 dump(세션 중 재실행만 선택)
{{PY}} .project_manager/tools/pm_handoff.py --dry-run         # 세션 종료 핸드오프
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run   # 엔진 동기화(메인테이너)
```

PM workflow skill(`/pm-bootstrap`·`/pm-handoff`·`/pm-wave-claim`·`/pm-dev-delegate`·`/pm-wave-finish`·`/pm-regression`·`/spike-new`)의 용법·backbone 단일 진실은 [`.claude/skills/`](.claude/skills/) 각 SKILL.md.

> **Windows/인코딩**: 엔진이 코드로 처리하므로 Windows/CP949·PowerShell 에서도 env 없이 한글이 동작한다. 필요 시 PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. `{{PY}}` 는 `board.py init` 이 PATH 탐지(Windows=`python`)로 채운다.
> **PowerShell 5.x `&&` 미지원**(ParseError) — `cd X && cmd` 대신 workdir 파라미터/명령 분리. Windows 진입은 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요).

## 7. 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py·ticket_finish.py·pm_*.py(숨김 — `ls -a`) |
| `.project_manager/wiki/` | 산출물(status·pm_state / pm_role·pm_playbook(lazy) / log / decisions / raw) |
| `.claude/skills/` · `.claude/agents/` | PM workflow skill · 서브에이전트(architect·developer·code-reviewer) |

## 막혔을 때 / lazy 참조 (happy-path 밖 → 그때 Read)

- 의존 미완·외부 키 없음 → `board.py block --reason "..."`; 잘못 claim → `board.py unclaim`.
- 복잡 운영(wave 충돌·incident·핸드오프 심층·멀티-PM clone·결정권한 경계·프레임워크 갱신) → [`.project_manager/wiki/pm_role.md`](.project_manager/wiki/pm_role.md)(정적 운영 매뉴얼) · [`.project_manager/wiki/pm_playbook.md`](.project_manager/wiki/pm_playbook.md)(Wave 패턴·메타 정책) Read.
- 모르는 구조 결정 → [`.project_manager/wiki/decisions/`](.project_manager/wiki/decisions/) 절차로 ADR 작성 후 진행.
