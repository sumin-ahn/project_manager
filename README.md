# Claude Project Framework

> **LLM 에이전트 기반 개발 운영 프레임워크** — 멀티 세션 ticket 보드(JIRA) + 문서 그래프 위키 +
> PM·Researcher·Architect·Dev·Reviewer 협업 구조 + PM workflow slash command 를 **도메인 무관
> 템플릿**으로 추출한 것. 새 프로젝트가 어댑터 한 트리를 복제하고 placeholder 만 채우면 같은
> 운영 프로세스를 그대로 쓸 수 있다.

이 프레임워크는 실전 멀티-에이전트 프로젝트에서 100+개 ticket·20+개 ADR 을 거치며 검증된 운영
계층을 추출한 것이다. "구조"와 "도메인 내용"이 처음부터 분리돼 설계됐기 때문에 이식 장벽이 낮다.

> **이 문서는 프레임워크 *공통 가이드*다** (네 기둥·도입·워크플로·이식성 — 하니스 무관).
> 하니스별 어댑터 세부는 각 타깃 README([`templates/claude_code/`](templates/claude_code/README.md) ·
> [`templates/opencode/`](templates/opencode/README.md)). 이 repo 를 *개발*(도그푸딩)하려면
> [`CLAUDE.md`](CLAUDE.md).

---

## 1. 무엇으로 이루어져 있나 — 네 기둥

| 기둥 | 무엇 | 핵심 파일 |
|---|---|---|
| **① Ticket 보드 (JIRA)** | 여러 LLM 세션이 충돌 없이 병렬 작업하는 가벼운 작업 보드. 디렉토리 = 상태, `mv` = atomic lock. | `.project_manager/tools/board.py` |
| **② 문서 그래프 위키 (3축)** | 진행 중 프로젝트의 운영 계층 — **architecture.md**(현재-아키텍처 단일 진실·부트스트랩 #1·ADR-0022) + **domain 지식 레이어**(그 *세부* — 살아있는 concept 그래프·`covers:` 코드 링크·소환/채록·freshness lint·ADR-0018) + decisions+spikes + process(상태·사양·일지). `[[wikilink]]` 인터링크 + frontmatter (Karpathy LLM-Wiki 패턴 계승). | `.project_manager/wiki/` (`architecture.md`·`domain/`·`decisions/`·…) |
| **③ PM·Researcher·Architect·Dev·Reviewer 협업** | PM 세션이 ticket 을 발행·분할하고 4축(gather=researcher / design=architect / build=developer / evaluate=code-reviewer)에 위임 (ADR-0019). **generate ≠ evaluate**, **design labor ≠ decision** — 주체를 분리하고 결정·비준·synthesis 는 PM. | 어댑터층(`.claude/agents/`·`.opencode/agents/`), `.project_manager/wiki/pm_role.md` |
| **④ PM workflow skill** | PM 의 반복 workflow (부트스트랩 / wave claim / 위임 / wave finish / 핸드오프) 를 trigger 단위로 강제. backbone CLI(`pm_*.py`) + 어댑터 slash command. | `.project_manager/tools/pm_*.py`, 어댑터층(`.claude/skills/`·`.opencode/command/`) |

설계 원칙 한 줄: **board.py 는 순수 ticket 도구, `.project_manager/wiki/` 는 문서 그래프 패턴,
PM·orchestrator 는 협업 모델, PM skill 은 trigger 단위 명시성 강제 — 넷 다 도메인을 모른다.**

---

## 2. 디렉토리 구조

엔진(`.project_manager/`)은 모든 타깃이 공유하고, 어댑터층만 하니스마다 다르다.

```
<프로젝트 루트>/
├── (진입 문서)                   # 하니스별 — claude_code: CLAUDE.md · opencode: AGENTS.md
├── .project_manager/             # 공유 엔진 (숨김 디렉토리 — ls -a)
│   ├── tools/
│   │   ├── board.py              # ① ticket 보드 CLI — 도메인 무관, 그대로 사용
│   │   ├── domain.py             # ② domain 지식 레이어 엔진 (covers·freshness)
│   │   ├── ticket_finish.py      # ④ ticket 완료 부기 자동화 (§6 주의)
│   │   ├── pm_bootstrap.py       # ④ PM 세션 시작 dump
│   │   ├── pm_handoff.py         # ④ PM 세션 종료 핸드오프
│   │   └── pm_log.py             # ④ log 의미단위 읽기 + 아카이브
│   └── wiki/                     # ② 문서 그래프 위키
│       ├── README.md             #   길찾기 + "디렉토리 의미" 단일 정의처
│       ├── architecture.md       #   현재-아키텍처 단일 진실 (① live / ② target · 부트스트랩 #1 · ADR-0022)
│       ├── status.md             #   활성 모듈 매트릭스 (모듈 진행상태·비고 — judgment-only·ADR-0023)
│       ├── board.md              #   ticket 현황 (board.py 자동 생성)
│       ├── log/                  #   작업 일지 — current.md(활성) + archive/(봉인)
│       ├── domain/               #   살아있는 지식 레이어 — concept/guide 페이지 (covers 로 코드 추적)
│       ├── pm_role.md            #   PM 인계 — 정적 핵심 (부트스트랩·결정 권한·안전 경계)
│       ├── pm_state.md           #   PM 인계 — 동적 상태 (세션 window·진행 중 의사결정)
│       ├── pm_playbook.md        #   PM 활동별 레퍼런스 (위임·Wave·효율 규칙) — lazy
│       ├── tickets/              #   open/ claimed/ blocked/ done/ + _template.md
│       ├── decisions/            #   ADR — 결정과 근거 (NNNN-slug.md + README 색인)
│       ├── specs/                #   사양 단일 진실 (포맷·한도·인터페이스)
│       ├── ideas/                #   pre-ADR 후보 (open/ promoted/ killed/)
│       └── raw/                  #   IMMUTABLE 시간 스냅샷 (plans·evaluations·benchmarks)
└── (어댑터층)                    # 하니스별 — 아래
```

어댑터층 = 그 하니스의 에이전트·skill 정의 + 진입 문서. 세부 구성은 각 타깃 README:

- **Claude Code** — `.claude/`(agents·skills·settings) + `CLAUDE.md`. [`templates/claude_code/README.md`](templates/claude_code/README.md)
- **opencode** — `.opencode/`(agents·command) + `AGENTS.md`. [`templates/opencode/README.md`](templates/opencode/README.md)

---

## 3. 도입 — PM 홈 생성 (표준) · 임베드 (`--into`)

> **표준 채택(ADR-0026·비임베드)**: `pm-import --new <home>` 로 **PM 홈**(코드 없는 board/wiki/엔진 홈)을 만들고,
> 아래 **[§8](#8-multi-repo-nm-운용--pm-config-파사드-adr-01100140016)** 으로 프로젝트 repo 를 attach(worktree) 한다.
> `--into`(임베드 — 기존 프로젝트 안에 `.project_manager` 동거)는 특정 케이스다. 즉 **홈 생성(§3) → 프로젝트 attach(§8)**
> 가 한 채택 서사다.

> LLM 에이전트가 *자율로* 채택을 수행한다면 → **[`ADOPT.md`](ADOPT.md)**(fresh 세션 온보딩·"경로/URL 하나만 주면 자율"). 아래 §3.1 은 사람이 직접 도입하는 경로.

### 3.1 권장 — `pm-import.sh` 파사드 (one-shot)

정규 경로는 **manager(프레임워크 checkout) 루트의 `pm-import.sh`(`/.cmd`)** 다 — 어댑터 트리 복사 ·
placeholder 치환 · `board.py init` · git init(`--new`) · (opencode) 모델 해소까지 한 번에 처리한다.
수동이 빠뜨리기 쉬운 단계까지 묶으므로 이쪽을 권장한다.

```bash
<manager>/pm-import.sh --new <dest>                    # Claude Code 타깃 (default harness)
<manager>/pm-import.sh --new <dest> --harness opencode # opencode 타깃
<manager>/pm-import.sh --into <dest>                   # 기존 프로젝트에 도입 (비파괴 · 충돌 백업)
<manager>/pm-import.sh --new <dest> --dry-run          # 적용 전 계획만 — 파일 미변경 (권장)
```

(Windows 는 `pm-import.cmd`. `--from` 은 manager 루트 auto-default — 생략 가능. import 시 manager
루트가 채택자 `local.conf` 의 `upstream=` 으로 기록돼, 이후 엔진 갱신은 `pm-config.sh update` 무인자로
받는다 — 상세 = [§5 *엔진 갱신 받기*](#엔진-갱신-받기-update).) 채택자는 모노레포 구조(`templates/`·루트
도그푸딩)를 몰라도 된다 — import 된 폴더 안에서 `.project_manager/` + 어댑터층이 그대로 자기 것이 된다.

### 3.2 수동 longhand (pm_import 이 자동화하는 절차)

파사드를 못 쓰는 환경이거나 각 단계를 직접 이해·제어하려면 손으로 한다 — pm_import 이 내부에서
하는 것과 동일하다 (예시는 claude_code 타깃):

```bash
# 1) 어댑터 트리를 새 프로젝트 루트로 복사 (dotfile 빠지지 않게 trailing dot /. 주의)
cp -r templates/claude_code/. /path/to/new-project/
cd /path/to/new-project/

# 1b) 의존성 설치 — fresh clone 은 이걸 먼저 깔아야 board.py·pytest 가 import 단계를 넘는다.
python3 -m pip install -r requirements-dev.txt   # PyYAML(런타임) + pytest(테스트)

# 2) placeholder 일괄 치환 (§4 표). pm_role.md·pm_playbook.md 는 제외 — 엔진(pm_update
#    동기화 대상)이라 {{PY}}·{{TEST_CMD}}·{{PROJECT_NAME}} 를 리터럴로 두고 local.conf 가
#    해소한다. 치환하면 다음 pm_update 때 되돌아간다.
grep -rl '{{' . --include='*.md' --include='*.json' --include='*.sh' --include='*.py' | \
  grep -vE 'wiki/pm_role\.md|wiki/pm_playbook\.md' | \
  xargs sed -i \
    -e 's|{{PROJECT_NAME}}|My Project|g' \
    -e 's|{{PROJECT_TAGLINE}}|한 줄 프로젝트 설명|g' \
    -e 's|{{PROJECT_ROOT}}|/path/to/new-project|g' \
    -e 's|{{PY}}|python3|g' \
    -e 's|{{TEST_CMD}}|python3 -m pytest tests/ -q|g' \
    -e "s|{{DATE}}|$(date +%F)|g"

# 3) 이 clone 등록 (clone 당 1회) — solo(N=1·M=1) 또는 multi-repo(N×M·ADR-0016·§multi-repo)
python3 .project_manager/tools/board.py init                        # solo: legacy T-NNNN
#   multi-repo(M>1·prefix 네임스페이스): board.py init --prefix PAY --area "결제"   # → T-PAY-NNN

# 4) board.py 동작 확인 — 첫 ticket 발행
python3 .project_manager/tools/board.py new "첫 ticket — 환경 셋업 검증" --tag infra
python3 .project_manager/tools/board.py list

# 5) free-form placeholder 직접 채우기 (sed 로 안 되는 서술 항목 — §4):
#    - {{PROJECT_CONSTRAINTS}} → 진입 문서(CLAUDE.md/AGENTS.md §프로젝트 고유 제약) — 단일 거처 (어댑터는 operational 전용)
#    - {{PROTECTED_PATHS}}·{{USER_GATE_ITEMS}} → .project_manager/wiki/pm_role.local.md (overlay)
#      — 파일 안 <!-- TODO --> 참고.

# 6) (Python 외 언어면) local.conf 의 test_cmd + ticket_finish.py / pm_*.py 의 pytest 가정 교체 (§6).

# 이후 프레임워크 개선 받기: pm_update.py --from <upstream-checkout> [--dry-run]   (상세 = §5 엔진 갱신 받기)
```

치환 후 남은 `{{...}}` 확인 (단, `pm_role.md`·`pm_playbook.md` 의 `{{PY}}`·`{{TEST_CMD}}`·`{{PROJECT_NAME}}` 는 **의도적으로 남는다** — local.conf 가 해소):

```bash
grep -rn '{{' . --include='*.md' --include='*.json' --include='*.sh' --include='*.py'
```

---

## 4. Placeholder 표

`sed` 로 일괄 치환 가능한 토큰:

| 토큰 | 의미 | 예시 |
|---|---|---|
| `{{PROJECT_NAME}}` | 프로젝트 표시 이름 | `My Project` |
| `{{PROJECT_TAGLINE}}` | 한 줄 프로젝트 설명 | `한 줄 프로젝트 설명` |
| `{{PROJECT_ROOT}}` | 프로젝트 루트 절대경로 | `/home/user/workspace/myproject` |
| `{{PY}}` | Python 실행 prefix | `venv/bin/python` 또는 `python3` |
| `{{TEST_CMD}}` | 전체 회귀 명령 | `venv/bin/python -m pytest tests/ -q` |
| `{{DATE}}` | 초기화 날짜 (wiki frontmatter) | `2026-05-22` |

> ⚠️ `{{PY}}`·`{{TEST_CMD}}`·`{{PROJECT_NAME}}` 은 **엔진 문서(`pm_role.md`·`pm_playbook.md`)에선 치환하지 않는다** — `local.conf` 가 해소(`board.py init` 기록)하고 pm_update 동기화 대상이라 치환하면 되돌아간다. 다른 파일(진입 문서 등)에선 sed 로 채워도 됨.
> opencode 타깃은 추가로 `{{OPENCODE_PRO_MODEL}}`(subagent 모델 ID)을 가지며 — sed 가 아니라 pm_import 의 결정적 `opencode models` 조회로 해소된다 ([`templates/opencode/README.md`](templates/opencode/README.md) §모델 선택).

직접 서술해야 하는(자유 형식) placeholder — 파일 안 `<!-- TODO -->` 주석으로 표시:

| 토큰 | 어디에 | 무엇을 채우나 |
|---|---|---|
| `{{PROJECT_CONSTRAINTS}}` | 진입 문서(`CLAUDE.md`/`AGENTS.md` §프로젝트 고유 제약) — 단일 거처 | 프로젝트의 **절대 위반 금지 제약**. 아키텍처 불변식·안전 경계 등. (예: "핵심 결정 로직 ↔ 비결정/LLM 계층 경계 분리", "외부 호출은 fail-soft") |
| `{{PROTECTED_PATHS}}` | **`pm_role.local.md`** §보호 영역 (어댑터엔 이 거처를 가리키는 정적 포인터만) | 서브에이전트·PM 이 **건드리면 안 되는 파일/디렉토리**. (예: 운영 한도·안전 상수 config, immutable `raw/` 스냅샷) |
| `{{USER_GATE_ITEMS}}` | **`pm_role.local.md`**(overlay) | PM 자율 결정 밖 — **사용자 사전 동의가 필요한 행위**. (예: 외부 비가역 행위, 유료 API 대량 호출) |

### 방법론 vs 누적 학습 분리 (ADR-0007)

- **`pm_playbook.md` = 순수 방법론** (프로젝트 무관: wave 절차·회귀 위생·운영 효율 규칙 등).
  엔진 문서이므로 `pm_update` 가 자동 갱신한다 — 직접 도메인 내용을 박지 않는다.
- **이 프로젝트의 누적 wave 학습·도메인 사례 → `pm_playbook.local.md`** (인스턴스 소유·manifest 밖·tracked).
  `pm_import` 로 도입하면 이 빈 스텁이 자동 생성된다(재-import 에서도 기존 내용 비파괴 보존).

> ⚠️ **규약 — `agents`/`skills`/`pm_playbook.local` 등 placeholder 를 *fill 하는 순간* 그 파일은
> `engine.manifest` 밖으로 둔다.** 안 그러면 다음 `pm_update` 가 무치환 raw overwrite 로 그 fill 을
> 덮어쓴다. 방법론 본문은 manifest 안(synced)에, 인스턴스가 채운 학습은 manifest 밖(인스턴스 소유)에.

---

## 5. 핵심 워크플로 (도입 후 일상)

### Ticket 보드
```bash
{{PY}} .project_manager/tools/board.py list --status open      # 잡을 수 있는 ticket
{{PY}} .project_manager/tools/board.py show T-0001             # 한 ticket 상세
{{PY}} .project_manager/tools/board.py claim T-0001 --session A   # atomic claim
{{PY}} .project_manager/tools/board.py complete T-0001 --tests-pass
{{PY}} .project_manager/tools/board.py new "제목" --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint                    # 의존성·thin-ticket 검사
{{PY}} .project_manager/tools/board.py idea new "후보 아이디어"   # pre-ADR 아이디어
```

### PM·Researcher·Architect·Dev·Reviewer 루프 + PM skill

PM 세션은 **wave** 단위로 ticket 을 처리한다. Wave = 사용자 명시 *"wave 진행"* / *"최대한 많이 진행"*
신호에 PM 이 자율로 묶어 처리하는 작업 단위 (보통 1~여러 ticket). 매 wave 사이 사용자 게이트 없이
다음 wave 로 이어지며, 사용자 게이트 항목이 섞이면 wave 중단·사용자 결정 대기. 한 wave 의 5 단계는 각
slash command 하나가 trigger 한다 (자세한 wave 구성은 `pm_playbook.md` §"Wave 패턴"):

1. **`/pm-bootstrap`** — 세션 시작 시 board·git·log dump. PM 손은 *직전 세션 요약 / 옵션 제시* 만.
2. **`/pm-wave-claim T-NNNN`** — DoD self-containment·depends_on·placeholder 검증 후 claim.
3. **`/pm-dev-delegate T-NNNN`** — developer 서브에이전트 위임 → 구현.
4. **`/pm-dev-delegate T-NNNN --role code-reviewer`** — 독립 검토 (generate ≠ evaluate). must-fix 있으면 재작업.
5. **`/pm-wave-finish T-NNNN <섹션>`** — 회귀+status+log+board+stage. git commit 은 PM 손.

PM 은 build/evaluate 외에 **gather·design** 도 서브에이전트에 위임한다 (4축 = ADR-0019 · 위 개요표 ③):

- **researcher (gather)** — bounded 사실 수집 (여러 파일·로그·레퍼런스를 훑어 사실·인용·목록 추출). ticket
  분할 전 사실 정찰이나 *왜 이런가* 조사에. 읽기 전용 (조사≠결정).
- **architect (design · Opus)** — 설계 노동 (idea triage / ADR 초안 / spec 추출 / 인터페이스 설계 / 가설
  검증 / architecture.md·domain content-truth 유지). **design labor ≠ decision** — architect 는 권고+초안까지,
  채택·발행·비준은 PM (ADR-0022 · ADR-0024 외부 설계 교차검토 게이트).

세션 종료 시 **`/pm-handoff`**. board.py claim/complete 와 status.md·log/current.md 갱신은
**PM(orchestrator) 담당** — 서브에이전트는 gather/design/build/evaluate 만 한다. (위임 *기제*는 하니스마다
다르다 — claude_code 는 `Agent` 툴 `subagent_type`, opencode 는 네이티브 `task` tool. 각 타깃 README 참조.)

### domain 지식 레이어 (살아있는 프로젝트 지식)

`.project_manager/wiki/domain/` = 이 프로젝트가 **무엇이고 어떻게 다루나**의 *살아있는* 지식 그래프.
`decisions/`(왜·동결)와 대비해 *현재 무엇·어떻게*를 계속 갱신한다. **`architecture.md`(현재-아키텍처
단일 진실·부트스트랩 #1·ADR-0022)와 공존하는 그 *세부* 지식층**이다 — architecture.md 가 구조·모듈·
구현상태를 한 장으로 잡고, domain 페이지가 `covers:` 코드 글롭 단위로 그 세부(개념·절차·조사)를 깊게
편다 (refines ADR-0018). architecture↔domain 충돌은 *의도↔현실 드리프트*를 표면화하는 기능이다.

**페이지 작성:**
```bash
cp .project_manager/wiki/domain/_template.md .project_manager/wiki/domain/<주제>.md
# frontmatter 채움:
#   type:    concept(무엇·왜) | guide(어떻게·절차) | research(조사·누적)
#   covers:  이 페이지가 담당하는 코드 글롭 (예: src/foo/**). 코드-무관 개념이면 비움.
#   derived: false(사람 author) | true(코드서 자동생성·손대지 마)
```
한 페이지 = 한 가지. `[[다른-페이지]]` 로 링크 → 그게 곧 그래프 (wikilink lint 가 검증).

**CLI (`domain.py`):**
```bash
{{PY}} .project_manager/tools/domain.py list                    # 페이지 카탈로그 (type·covers·stale)
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN  # ticket touches 와 겹치는 covers 페이지 (소환)
{{PY}} .project_manager/tools/domain.py capture --tickets T-NNNN  # touch∩covers 갱신 reminder (채록)
{{PY}} .project_manager/tools/domain.py lint                    # freshness — stale 페이지 검사
```

**살아있는 루프:** 코드 touch → 겹치는 페이지 **소환**(`domain affected`) → 갱신 reminder
(`domain capture`) → 채록 → `covers` 코드가 페이지 `updated` *후* 바뀌면 **stale** ⚠ 로
가시화(`domain lint`). *막지 않고 보이게* — 틀린 정보 조용히 참조 방지.

### 엔진 갱신 받기 (Update)

도입 후 upstream(프레임워크) 엔진 변경을 흡수한다. 채택자 루트에서:
```bash
cd <project> && ./pm-update.sh          # facade — manifest 경로만 byte-overwrite (PM 세션이면 /pm-update 스킬 권장)
```
- **upstream 출처** = `local.conf upstream=`(pm_import 이 자동 기록). **URL**(릴리스 추적·기본) 또는 **로컬 경로**(엔진 공동개발). URL 은 엔진이 직접 fetch 안 한다(파일-복사·ADR-0032 D5) — `/pm-update` 스킬이 cache clone/fetch 후 `--from <cache>`. 경로는 `./pm-update.sh` 가 직접.
- **무엇이 바뀌나** = `engine.manifest` 경로만(엔진·`@render` 어댑터). 인스턴스 상태(board·status·log·decisions·README·진입문서 customization)는 manifest 밖이라 **안 받는다**(구조적 비충돌).
- **무엇이 올지 미리보기** — sync 전에 baseline↔upstream HEAD 변경점을 본다:
  ```bash
  ./pm-update.sh --changes [--from <checkout>]   # commit 수 + 받을 엔진파일(엔진 영향)/그 외 분리·read-only
  ```
  채택자 `local.conf` 의 `upstream_rev`(마지막 동기 baseline)부터 upstream HEAD 까지 — "내가 받은 버전 이후 무엇이 얼마나 바뀌었나". 변경 0이면 받을 게 없다(최신).
- **drift 경고** — `board.py lint` 의 `adapter-drift`(advisory·never-block)가 "upstream 이 baseline 이후 앞섰다"를 표면화. 보이면 위 절차로 동기.
- **multi-repo(N×M)** = `<manager>/pm-config.sh update`([§8](#8-multi-repo-nm-운용--pm-config-파사드-adr-01100140016)).
- **재import** — opencode `.opencode/*` 같은 `@target-owned` 어댑터는 update 채널 밖이라 **재import 로 받는다**(§3 의 `--into`). claude 스킬은 pm-update 가 전파.

> PM 세션은 `/pm-update`(갱신·freshness 자동분기·drift 표면화를 묶음)·`/pm-env`(upstream 값 전환·repo/worktree 관리)를 쓴다.

### 외부 코드리뷰 (선택 · 기본 OFF · ADR-0004)

내부 `code-reviewer` 서브에이전트(무료·맥락 깊음)와 **상보적**으로, 코드 변경분을 *외부* 리뷰어(codex
등)에 보내 독립 시점·다른 모델의 2차 리뷰를 받는 도구 (`.project_manager/tools/external_review.py`).

> ⚠️ **코드 diff 가 외부로 전송된다.** 한 번 전송하면 회수 불가 → **기본 비활성**. 켤지는 프로젝트가 결정한다.

- **켜기:** `board.py init` / `pm_update` 시 opt-in 프롬프트(비대화형이면 안전쪽 OFF), 또는 `local.conf external_review_enabled=true`.
- **외부 도구 교체:** `local.conf reviewer_cmd=...` (기본 codex). 다른 CLI 로 갈아끼울 수 있다.
- **프로젝트 맥락:** `.project_manager/review_context.local.md`(인스턴스 소유). 엔진 도구엔 도메인 0.
- **시크릿 보호:** `.env`·`*secret*`·`*.key`·`*token*`·`*.pem` 등 자동 제외. `local.conf review_denylist_extra=...` 로 추가.
- **안전 가드:** read-only · fail-soft(외부 실패 시 내부 code-reviewer 폴백) · must-fix 감지 시 exit 1.

---

## 6. 이식성 등급 — 무엇이 그대로고 무엇을 고쳐야 하나

| 구성요소 | 이식성 | 비고 |
|---|---|---|
| `.project_manager/tools/board.py` | ✅ 그대로 | 순수 ticket 도구. 하드코딩 경로 없음. Python 3 + `pyyaml` 만 필요. |
| `pm_bootstrap.py` | ✅ 그대로 | PM 세션 시작 dump. timezone (KST default) 만 맞춰. |
| `pm_handoff.py` | 🟡 pm_state.md / pm_playbook.md 형식 결합 | sliding window·인계 프롬프트 추출이 해당 절 형식에 정규식으로 묶임 — 형식 바꾸면 정규식도 같이. |
| `pm_log.py` | ✅ 그대로 | log 의미단위 읽기 + 아카이브. entry 경계(`## [YYYY-MM-DD]`)만 의존. |
| `.project_manager/wiki/` 골격 | ✅ 구조 재사용 | README·sub-README·`_template.md`(domain 포함) 는 도메인 무관. status·domain/ 페이지만 새로 채움. |
| 어댑터층 (`.claude/`·`.opencode/`) | ✅ 거의 그대로 | researcher·architect·developer·code-reviewer + PM workflow. operational 토큰 전용(free-form 0) — 고유 제약은 root doc, 보호 영역은 `pm_role.local.md` §보호 영역에서 채움. 세부는 각 타깃 README. |
| `pm_role.md` | ✅ 도메인 무관 | PM 정적 핵심. `{{USER_GATE_ITEMS}}` (→`pm_role.local.md`)만 채움. |
| `pm_state.md` | ✅ 구조 재사용 | PM 동적 상태. 세션 window 는 `/pm-handoff` 가 자동 갱신. |
| `pm_playbook.md` | ✅ 도메인 무관 | PM 활동별 레퍼런스. 누적 학습은 `pm_playbook.local.md` 로 분리(ADR-0007). |
| 진입 문서 (`CLAUDE.md`·`AGENTS.md`) | 🟡 템플릿 | 부트스트랩 패턴 재사용, 프로젝트 한 줄·제약은 placeholder. |
| `ticket_finish.py` | 🟡 **Python+pytest 결합** | status.md 의 정확한 라인 형식에 정규식 앵커. **선택 도구** — 없어도 board.py 만으로 완결. Python 외 언어면 pytest 파싱 교체. |
| `external_review.py` | 🟡 **선택 · 외부 전송 · 기본 OFF** | 외부 리뷰어 어댑터(ADR-0004). `external_review_enabled=true` opt-in 필요. 없어도 내부 code-reviewer 로 완결. |
| `run_tests_hook.sh` | 🟡 언어별 교체 | `pytest` 호출을 해당 언어 러너로. |

---

## 7. 설계 출처 / 계보

- **Ticket 보드** — 디렉토리=상태 + POSIX `rename(2)` atomic lock. 분산 락이 아닌 의도된 단순성
  (한 머신·한 클론 기준). 자세히는 [`tickets/README.md`](.project_manager/wiki/tickets/README.md).
- **문서 그래프 위키** — Andrej Karpathy 의 LLM Wiki 패턴 계승 — `[[wikilink]]` 인터링크 + YAML
  frontmatter + append-only `log/current.md`. 단, 정적 KB 가 아니라 **ticket 주도로 자라는
  엔지니어링 운영 계층**으로 재정의했다. `entities/`·`concepts/` 류 KB 디렉토리는 의도적으로 뺐다.
- **PM·orchestrator 위임** — `generate ≠ evaluate`. 구현 주체와 검토 주체를 분리해 구현자의 맹점을 잡는다.
- **PM workflow skill** — Junu Jeon "How to Ride Your Horse" 의 SDLC skill chain 패턴에서 영감.
  우리 식 변형: **skill 의 진짜 가치 = 자동화 부산물이 아니라 명시성 강제 메커니즘** — ticket
  self-contained 의무·DoD verify-able 같은 규율을 매 trigger 마다 강제한다.

### 프레임워크 목표 (방향성)

**개발·관리 프로세스 자동화로 사용자 개입 최소화** — 프로젝트가 사용자 확인 없이도 스스로 구현·발전해
나가게 한다. 그래서 PM 자율 영역을 의도적으로 넓히고, skill 시스템으로 trigger 단위 강제 규율을 둔다.
단, **자동화 대상은 개발·관리 프로세스에 한정** — 도메인의 비가역·미션 결정(자본·안전 한도·외부 송신
같은 되돌릴 수 없는 행위)은 자동화 비대상·영구 사용자 게이트. 그 경계는 도입 프로젝트의 `pm_role.md` /
`pm_role.local.md` §"사용자 게이트" / §"금지" 가 명시한다.

---

## 8. multi-repo (N×M) 운용 — `pm-config` 파사드 (ADR-0011·0014·0016)

> [§3](#3-도입--pm-홈-생성-표준--임베드---into) 에서 PM 홈을 만든 뒤, 여기서 프로젝트 repo 를 attach 한다 —
> *홈 생성 → 프로젝트 attach* 단일 채택 서사(ADR-0026 비임베드). 홈은 M=1 이어도 worktree 로 프로젝트를 잡는다.

모드 = **multi-PM(N 세션 × M repo·ADR-0016)** 한 개념. N=1·M=1 = 옛 solo(슬롯 오버헤드 0). 한
*사용자*가 여러 repo 를 묶어 운용할 때(M>1·옛 '우산' = **single-user multi-repo** 로 재정의·ADR-0016
가 ADR-0011 amend), 셋업·조회·진단은 루트의 `pm-config.sh`(`/.cmd`) 한 파사드로 한다:

```bash
<manager>/pm-config.sh repo add <name> --git <url> --test "<cmd>"  # 패밀리에 repo 등록 + .repos clone
<manager>/pm-config.sh worktree add <repo>                         # 새 worktree 슬롯 + submodule init
<manager>/pm-config.sh status | whoami                             # 풀/리스 + 이 세션 repo/슬롯/branch
<manager>/pm-config.sh release <slot> [--force]                    # 작업완료 반납 / 수동 강제(백스톱)
<manager>/pm-config.sh update [--from <upstream>]                  # 엔진 갱신 (pm-update 흡수)
```

셋업·조회·진단 전용이다 — 런타임 worktree alloc/release 자동화는 `pm-bootstrap`/handoff 가 하고,
`pm-config release` 는 수동 반납/강제(백스톱)만. 브랜치 할당은 `pm-bootstrap <repo> --branch <B>`
소관. 솔로(M=1)는 이 파사드를 안 써도 된다 — board/tools 현행 그대로(additive).

---

## 9. 의존성

- Python 3.9+ (board.py 는 `from __future__ import annotations` 로 3.9 호환). **Windows 는 런처 `py`**
  (`py -3.12`) — `python`/`python3` 은 WindowsApps 가짜 shim 일 수 있다. 인코딩은 엔진이 코드로 처리
  (파일 IO `encoding="utf-8"`·콘솔 reconfigure)하므로 env 없이 동작 — cp949·PowerShell 서도 한글 깨짐 0.
- 의존성 선언: `requirements.txt`(런타임=`PyYAML>=6`) + `requirements-dev.txt`(`-r requirements.txt` + `pytest`).
  설치: `python3 -m pip install -r requirements-dev.txt` (Windows: `py -3.12 -m pip install ...`).
- `pyyaml` — `board.py` 의 frontmatter 파싱. `jq` — `run_tests_hook.sh` (선택 hook). `codex`(또는
  `reviewer_cmd`) — `external_review.py` 사용 시에만 (선택·기본 OFF).
- LLM 코딩 에이전트 (Claude Code·opencode 등) — 어댑터층 에이전트·skill, 위임 툴.

board.py·ticket_finish.py·pm_bootstrap.py·pm_handoff.py 자체는 LLM 을 호출하지 않는 순수 결정론 도구다.
