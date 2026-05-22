# Claude Project Framework

> **LLM 에이전트 기반 개발 운영 프레임워크** — 멀티 세션 ticket 보드(JIRA) +
> 문서 그래프 위키 + PM·Developer·Reviewer 협업 구조를 **도메인 무관 템플릿**으로
> 추출한 것. 새 프로젝트가 이 디렉토리를 복제하고 placeholder 만 채우면 같은
> 운영 프로세스를 그대로 쓸 수 있다.

이 프레임워크는 Finance Agent 프로젝트(한·미 주식 자동매매 멀티 에이전트
시스템)에서 65개 ticket·14개 ADR 을 거치며 검증된 운영 계층을 추출한 것이다.
"구조"와 "도메인 내용"이 처음부터 분리돼 설계됐기 때문에 이식 장벽이 낮다.

---

## 1. 무엇으로 이루어져 있나 — 세 기둥

| 기둥 | 무엇 | 핵심 파일 |
|---|---|---|
| **① Ticket 보드 (JIRA)** | 여러 Claude 세션이 충돌 없이 병렬 작업하는 가벼운 작업 보드. 디렉토리 = 상태, `mv` = atomic lock. | `tools/board.py` |
| **② 문서 그래프 위키** | 진행 중 프로젝트의 운영 계층 — 상태·결정·사양·아이디어·일지를 markdown 그래프로. `[[wikilink]]` 인터링크 + frontmatter (Karpathy LLM-Wiki 패턴 계승). | `project_wiki/` |
| **③ PM·Dev·Reviewer 협업** | PM(orchestrator) 세션이 ticket 을 발행·분할하고, developer / code-reviewer 서브에이전트에 위임. **generate ≠ evaluate** — 구현자와 검토자를 분리. | `.claude/agents/`, `project_wiki/pm_role.md` |

설계 원칙 한 줄: **board.py 는 순수 ticket 도구, project_wiki/ 는 문서 그래프
패턴, PM·orchestrator 는 협업 모델 — 셋 다 도메인을 모른다.**

---

## 2. 디렉토리 구조

이 `project_manager/` 트리는 **새 프로젝트 루트에 그대로 복사**할 형태다.

```
<프로젝트 루트>/
├── CLAUDE.md                     # Claude Code 세션 진입점 (자동 로드)
├── tools/
│   ├── board.py                  # ① ticket 보드 CLI — 도메인 무관, 그대로 사용
│   └── ticket_finish.py          # PM 부기 자동화 헬퍼 (선택 — §6 주의)
├── .claude/
│   ├── agents/
│   │   ├── developer.md          # ③ 구현 서브에이전트 정의
│   │   └── code-reviewer.md      # ③ 검토 서브에이전트 정의
│   ├── settings.local.json       # 권한 + PostToolUse 테스트 hook
│   └── run_tests_hook.sh         # 파일 편집 시 회귀 자동 실행 hook
└── project_wiki/                 # ② 문서 그래프 위키
    ├── README.md                 #   길찾기 + "디렉토리 의미" 단일 정의처
    ├── status.md                 #   모듈 상태·테스트 수 단일 진실
    ├── board.md                  #   ticket 현황 (board.py 자동 생성)
    ├── log.md                    #   작업 일지 (append-only)
    ├── architecture.md           #   Layer·모듈 의존성 구조
    ├── pm_role.md                #   PM 세션 인계 문서 (운영 규칙·결정 권한)
    ├── tickets/                  #   open/ claimed/ blocked/ done/ + _template.md
    ├── decisions/                #   ADR — 결정과 근거 (NNNN-slug.md + README 색인)
    ├── specs/                    #   사양 단일 진실 (포맷·한도·인터페이스)
    └── ideas/                    #   pre-ADR 후보 (open/ promoted/ killed/)
```

---

## 3. 새 프로젝트에 도입하는 법 (5 단계)

```bash
# 1) 이 트리를 새 프로젝트 루트로 복사 (.claude 같은 dotfile 포함 주의)
cp -r project_manager/. /path/to/new-project/

cd /path/to/new-project/

# 2) placeholder 일괄 치환 (§4 표 참고). 예:
grep -rl '{{' . --include='*.md' --include='*.json' --include='*.sh' | \
  xargs sed -i \
    -e 's|{{PROJECT_NAME}}|My Project|g' \
    -e 's|{{PROJECT_TAGLINE}}|한 줄 프로젝트 설명|g' \
    -e 's|{{PROJECT_ROOT}}|/path/to/new-project|g' \
    -e 's|{{PY}}|python3|g' \
    -e 's|{{TEST_CMD}}|python3 -m pytest tests/ -q|g' \
    -e "s|{{DATE}}|$(date +%F)|g"

# 3) board.py 동작 확인 — 첫 ticket 발행
python3 tools/board.py new "첫 ticket — 환경 셋업 검증" --tag infra
python3 tools/board.py list

# 4) project_wiki/ 의 {{PROJECT_CONSTRAINTS}} / {{PROTECTED_PATHS}} 등
#    free-form placeholder 를 직접 채운다 (sed 로 안 되는 서술 항목).
#    → CLAUDE.md, .claude/agents/*.md, pm_role.md 안의 <!-- TODO --> 주석 참고.

# 5) (Python 외 언어면) .claude/run_tests_hook.sh 와 ticket_finish.py 의
#    pytest 가정을 해당 언어 테스트 러너로 교체 (§6).
```

치환 후 남은 `{{...}}` 가 없는지 확인:

```bash
grep -rn '{{' . --include='*.md' --include='*.json' --include='*.sh'
```

---

## 4. Placeholder 표

`sed` 로 일괄 치환 가능한 토큰:

| 토큰 | 의미 | 예시 |
|---|---|---|
| `{{PROJECT_NAME}}` | 프로젝트 표시 이름 | `Finance Agent` |
| `{{PROJECT_TAGLINE}}` | 한 줄 프로젝트 설명 | `한·미 주식 자동매매 멀티 에이전트 시스템` |
| `{{PROJECT_ROOT}}` | 프로젝트 루트 절대경로 | `/home/user/workspace/myproject` |
| `{{PY}}` | Python 실행 prefix | `venv/bin/python` 또는 `python3` |
| `{{TEST_CMD}}` | 전체 회귀 명령 | `venv/bin/python -m pytest tests/ -q` |
| `{{DATE}}` | 초기화 날짜 (wiki frontmatter) | `2026-05-22` |

직접 서술해야 하는(자유 형식) placeholder — 파일 안 `<!-- TODO -->` 주석으로 표시:

| 토큰 | 어디에 | 무엇을 채우나 |
|---|---|---|
| `{{PROJECT_CONSTRAINTS}}` | `CLAUDE.md`, `agents/developer.md`, `agents/code-reviewer.md` | 프로젝트의 **절대 위반 금지 제약**. 아키텍처 불변식·안전 경계 등. (finance 예: "결정론 코어 vs LLM 분석층 분리", "LLM 호출은 fail-soft") |
| `{{PROTECTED_PATHS}}` | `agents/*.md`, `pm_role.md` | 서브에이전트·PM 이 **건드리면 안 되는 파일/디렉토리**. (finance 예: `risk/limits.py` 같은 Tier 4 운영 config, immutable `raw/`) |
| `{{USER_GATE_ITEMS}}` | `pm_role.md` | PM 자율 결정 밖 — **사용자 사전 동의가 필요한 행위**. (finance 예: 외부 비가역 행위, 유료 API 대량 호출) |

---

## 5. 핵심 워크플로 (도입 후 일상)

### Ticket 보드
```bash
{{PY}} tools/board.py list --status open      # 잡을 수 있는 ticket
{{PY}} tools/board.py show T-0001             # 한 ticket 상세
{{PY}} tools/board.py claim T-0001 --session A   # atomic claim
{{PY}} tools/board.py complete T-0001 --tests-pass
{{PY}} tools/board.py new "제목" --touches a.py,b.py --tag phase-1
{{PY}} tools/board.py lint                    # 의존성·thin-ticket 검사
{{PY}} tools/board.py idea new "후보 아이디어"   # pre-ADR 아이디어
```

### PM·Dev·Reviewer 루프
1. **PM 세션** — `project_wiki/pm_role.md` 를 첫 번째로 읽는다. ticket 발행·분할.
2. PM 이 `Agent` 툴로 `subagent_type: developer` 위임 → 구현.
3. PM 이 `subagent_type: code-reviewer` 위임 → 독립 검토 (generate ≠ evaluate).
4. must-fix 있으면 developer 재작업. 없으면 PM 이 회귀 verify → `board.py complete`.

board.py claim/complete 와 status.md·log.md 갱신은 **PM(orchestrator) 담당** —
서브에이전트는 구현·검토만 한다.

---

## 6. 이식성 등급 — 무엇이 그대로고 무엇을 고쳐야 하나

| 구성요소 | 이식성 | 비고 |
|---|---|---|
| `tools/board.py` | ✅ 그대로 | 순수 ticket 도구. 하드코딩 경로 없음 (`REPO` 를 `__file__` 로 해소). Python 3 + `pyyaml` 만 필요. |
| `project_wiki/` 골격 | ✅ 구조 재사용 | README·sub-README·`_template.md` 는 도메인 무관. status/architecture 내용만 새로 채움. |
| `.claude/agents/` | ✅ 거의 그대로 | 역할·제약·부트스트랩 구조는 도메인 무관. `{{PROJECT_CONSTRAINTS}}`·`{{PROTECTED_PATHS}}` 만 채움. |
| `pm_role.md` | ✅ 도메인 무관 | PM 운영 모델·결정 권한·핸드오프 절차. `{{USER_GATE_ITEMS}}` 만 채움. |
| `CLAUDE.md` | 🟡 템플릿 | 부트스트랩 패턴은 재사용, 프로젝트 한 줄·디렉토리 표는 placeholder. |
| `tools/ticket_finish.py` | 🟡 **Python+pytest 결합** | status.md 의 **정확한 라인 형식**에 정규식 앵커가 묶여 있다 (`전체 테스트: N / N 통과`, `합계`, `pytest tests/ -q`). 제공된 `status.md` 템플릿은 이 앵커와 일치하게 작성됨 — status.md 형식을 바꾸면 `ticket_finish.py` 의 `_RE_*` 정규식도 같이 바꿔야 한다. Python 외 언어면 pytest 파싱 로직 교체 필요. **선택 도구** — 없어도 board.py 만으로 완결적으로 동작한다. |
| `run_tests_hook.sh` | 🟡 언어별 교체 | `pytest` 호출을 해당 언어 테스트 러너로. |

---

## 7. 설계 출처 / 계보

- **Ticket 보드** — 디렉토리=상태 + POSIX `rename(2)` atomic lock. 분산 락이
  아닌 의도된 단순성 (한 머신·한 클론 기준). 자세히는
  [`project_wiki/tickets/README.md`](project_wiki/tickets/README.md).
- **문서 그래프 위키** — Andrej Karpathy 의 LLM Wiki 패턴 계승 — `[[wikilink]]`
  인터링크 + YAML frontmatter + append-only `log.md`. 단, 정적 KB(knowledge
  base) 가 아니라 **ticket 주도로 자라는 엔지니어링 운영 계층**으로 재정의했다
  (원 프로젝트 ADR-0013). `entities/`·`concepts/` 류 KB 디렉토리는 의도적으로 뺐다.
- **PM·orchestrator 위임** — `generate ≠ evaluate`. 구현 주체와 검토 주체를
  분리해 구현자의 맹점을 잡는다.

---

## 8. 의존성

- Python 3.9+ (board.py 는 `from __future__ import annotations` 로 3.9 호환)
- `pyyaml` — `board.py` 의 frontmatter 파싱. (`pip install pyyaml`)
- `jq` — `run_tests_hook.sh` 가 hook JSON 파싱에 사용 (선택 hook 미사용 시 불필요)
- Claude Code — `.claude/agents/` 서브에이전트, `Agent` 툴 위임

board.py·ticket_finish.py 자체는 LLM 을 호출하지 않는 순수 결정론 도구다.
