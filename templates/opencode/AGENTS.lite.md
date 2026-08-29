# AGENTS.md — opencode PM 어댑터 (lite 진입·경량·자족)

> 이 파일 + 공유 엔진(`board.py`) + `.claude/skills/` 모델 스킬 채널 + `.opencode/command/` 사람 슬래시 팔레트로 부트스트랩→ticket 발행→위임→finish를 운영한다. command 사본은 canonical SKILL.md에서 기계 생성한다. `pm_role.md`·`pm_playbook.md`는 happy-path 밖에서만 lazy Read한다.

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 0. opencode 실행 모델

- **build primary=PM(orchestrator).**
- **위임=네이티브 `task` tool.** `.opencode/agents/*.md` subagent를 별도 자식 세션(fresh ctx=200K, 정의된 model/권한)에서 실행한다. 폴백은 `opencode run` 외부 프로세스(§3).
- **엔진=공유 python**(`.project_manager/tools/*.py`). PM이 bash로 호출·해석하며 **엔진은 수정하지 않는다.**
- **인코딩**은 엔진이 파일·콘솔에서 처리하므로 PowerShell/CP949도 env prefix 불필요. 필요할 때만 PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`.
- **PowerShell 5.x `&&` 미지원**(ParseError): `cd X && cmd` 대신 workdir 파라미터/명령 분리. Windows 진입은 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요).

## 1. 부트스트랩 (세션 시작)

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `/pm-bootstrap` dump 한 번뿐이다.

1. **이 문서(AGENTS.md·lite 코어)** — 이미 로드된 프로젝트 규칙·형상.
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

세션명 canonical은 **`<repo>_<N>`**이다. `board.py ... --repo <repo> --slot <N>`을 쓰며 활성 lease 1개면 생략 가능하다. 위임 라벨은 `orch-dev-TNNNN`/`orch-review-TNNNN`. 첫 turn에는 board 1줄 + 직전 요약 3~5줄 + 다음 옵션 + *무엇부터?* 결정 요청을 보고한다. 기계 dump는 `pm_bootstrap.py`.

## 2. 작업 원칙 (반드시)

- **작은 단위→단계별 테스트.** 한 모듈=한 ticket=한 단계. 회귀 `{{TEST_CMD}}`가 green이어야 끝난다.
- ticket 요구만 최소 변경하고 명시적 풀네임을 쓴다.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 아키텍처 불변식·안전 경계. 위임 프롬프트(§3)에도 인용. 없으면 이 절 삭제. -->

## 3. 위임 규약 (네이티브 `task` tool — 1차)

`claim → 위임(dev) → 검토(reviewer) → finish`. PM은 직접 구현하지 않는다.

- **1차=`task` tool** — `subagent_type`(=`developer`|`code-reviewer`|`architect`)·`description`(한 줄)·`prompt`(role 프롬프트)를 전달한다. opencode가 `.opencode/agents/*.md` subagent를 별도 자식 세션(fresh ctx·200K)에서 실행한다. 권한·모델은 subagent `permission:`/`model:`이 정하므로 `--agent`/`-m` 분기 불필요.
- role: developer=쓰기(코드+테스트), code-reviewer=독립 검토+라운드 파일 쓰기(제품 코드 수정 금지), architect=설계+라운드 파일 쓰기. 각 역할은 위임 프롬프트가 지정한 `NN-<역할>.md` 하나에만 쓴다.
- **사전조건:** ticket claim(canonical `<repo>_<N>`), depends_on done, touches 명시, 검증 가능한 DoD. **병렬은 touches가 disjoint일 때만** 한다.
- dev prompt: "T-NNNN 구현. 본문 단일진실(`board.py show T-NNNN`). board/status/log 는 PM — 너는 코드+테스트. 보고: 변경파일·테스트수·회귀결과·DoD evidence."
- reviewer 후 PM 직접 fix(1줄·1패턴)/dev 재작업(여러 줄)/별도 ticket(범위 외). reviewer의 should-fix도 흐름 cross-check한다.
- **폴백**(headless·CI·task tool 미노출): `opencode run --agent <developer|architect|code-reviewer> --format json "<프롬프트>"`. 역할 카드는 `mode: all`이라 primary 실행에서도 같은 `model:`/`permission:`을 쓰며 build/plan으로 축약하지 않는다. `pm_delegate.py` cross 실행은 카드가 없는 타 하네스 adopter에서도 같은 역할을 런타임 config로 주입한다. 병렬 폴백은 세션 DB 락 가능성이 있어 순차가 안전하다.

## 4. ticket 발행 계약 (PM 자족 — board.py new)

```bash
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --depends T-0001 --tag phase-1
```

본문 표준 절: **목표**(무엇·왜), **인터페이스**(시그니처·경로), **결정**(분기 근거), **완료 조건(DoD)**(테스트 green·산출·판정/비고를 검증 가능하게), **참고**(실재 `[[wikilink]]`, frontmatter `depends_on`/`blocks`). `board.py lint`는 목표·완료 조건·참고만 thin 차단(`_REQUIRED_SECTIONS`)하고 dangling을 검사한다. **본문만으로 구현 가능한 단일 진실이어야 한다.** 채번·area prefix·thin 세부는 필요 시 `pm_playbook.md`를 Read한다.

## 5. 완료 기록 (PM 손)

```bash
{{PY}} .project_manager/tools/ticket_finish.py --cluster C-<이름> [--repo <repo> --slot <N>]
```

`status.md` 모듈 행·`log/current.md` entry를 갱신하고 회귀 `{{TEST_CMD}}` green을 확인한 뒤 경로를 명시해 커밋한다:
`git commit -m "T-NNNN — <요약>" -- <ticket touches> .project_manager/wiki/status.md .project_manager/wiki/log/current.md .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md .project_manager/wiki/tickets/done/T-NNNN-<slug>.md`

bare commit은 남이 stage한 변경도 싣는다. 티켓 이동은 옛·새 경로를 모두 지정한다. **신규 파일은 `git add` 선행**: 미추적 pathspec은 `pathspec … did not match` rc=1을 낸다. 메시지에 `Co-Authored-By` 트레일러를 둔다. `.opencode/command/`의 `/pm-…` 슬래시 진입이 canonical `.claude/skills/` 내용을 호출해 스칼라·skeleton·stage를 자동화한다.

## 6. 결정 권한 (요약)

- **자율+사후 log** — ticket 발행/분할·depends_on 변경·block/unblock·spec 추출·일상 ADR(`scope: internal-process`)·위임.
- **사용자 게이트(사전 동의)** — 미션·핵심 안전 경계·유료/한도 API 대량·키 발급·외부 게시·배포·`scope: mission` ADR.
- **금지(단독 불가)** — 미션 변경·안전 경계 약화·영구 수동 영역 자동화. 양측 합의+ADR 필요. 상세는 [[pm_role.local.md]].

## 7. 라이브 외부 행위 안전 가드

- 단위 테스트는 전부 mock, 라이브 외부 호출은 통합 마커로만 한다. **프로덕션 진입점 라이브 실행 금지**; mock 자동 테스트로 검증한다.
- 외부 비가역 행위(송신·배포·키 발급) ticket은 사용자 명시 승인 후 진행한다. 새 비가역 행위에는 opt-in env 코드 안전 가드를 둔다.

## 8. 자주 쓰는 명령 / 핵심 디렉토리

```bash   # 엔진이 인코딩을 코드로 처리 — env prefix 불필요
{{PY}} .project_manager/tools/board.py list|show|claim|complete|new|lint
{{PY}} .project_manager/tools/pm_log.py tail
{{PY}} .project_manager/tools/pm_handoff.py --dry-run            # 핸드오프
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run   # 엔진 동기화(메인테이너)
```

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | 공유 엔진 board.py·pm_*.py(0 수정) |
| `.project_manager/wiki/` | status·pm_state/domain(`domain.py`)/pm_role·pm_playbook(lazy)/log/decisions/raw |
| `.claude/skills/` · `.opencode/command/` · `.opencode/agents/` | canonical 모델 스킬 · 기계 생성 slash `/pm-…` 팔레트 · subagent(`task` 위임 1차) |
| `AGENTS.md` | 이 파일(=claude_code의 CLAUDE.md lite) |

## 9. 막혔을 때 / lazy 참조 (happy-path 밖 → 그때 Read)

- 의존 미완·키 없음 → `board.py block --reason`; 잘못 claim → `board.py unclaim`; 본문 부족 → 보강 후 계속.
- 위임 실패(`task` 결과 또는 폴백 exit≠0)·결과 불완전 → 재위임 전 본문·컨텍스트 예산 점검.
- domain 지식 레이어(`domain.py list/affected/capture/lint`, stale 가시화) → full [`AGENTS.md`](AGENTS.md) §10.
- wave 충돌·incident·핸드오프 심층·멀티-PM·결정권한 경계·프레임워크 갱신 → [`pm_role.md`](.project_manager/wiki/pm_role.md)·[`pm_playbook.md`](.project_manager/wiki/pm_playbook.md) Read.
- 모르는 구조 결정 → `.project_manager/wiki/decisions/`에 ADR.

## 참고

- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실(lazy)
- `.project_manager/wiki/pm_playbook.md` — Wave 패턴·메타 정책(lazy)
- ADR-0006 — opencode 어댑터 결정(위임·인코딩·모델·self-driven)
