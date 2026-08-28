# CLAUDE.md — Claude Project Framework 공개 제품 (① canonical 엔진)

> 이 repo(①)는 **Claude Project Framework 의 공개 제품** `project_manager` 다 — canonical 엔진
> (`.project_manager/tools/`) + 방법론(`wiki/pm_role.md`·`pm_playbook.md`·`_template`·`domain/`) +
> 어댑터(`templates/`) + `tests/`. 도그푸딩 **PM 운영**(board·wiki·ADR·roadmap·dev-state)은 별도 ② repo
> `project_manager_dev` 가 **adopter#0** 로 수행하며, 이 worktree 가 ② 의 working checkout
> `work/project_manager_1` 이다. **엔진은 여기서 고치고 `tests/` 로 검증한다**
> ("고치는 곳 = 테스트하는 곳"). (채택자용 출하 템플릿은 `templates/` 직계 하위의 **모든 타깃** — 현재
> `templates/claude_code/`·`templates/codex/`·`templates/opencode/` —
> 그쪽 CLAUDE.md/AGENTS.md 는 출하 스캐폴드이고, 이 문서는 *엔진 개발자(=PM 세션)* 용이다.)

## 새 세션 부트스트랩

1. **이 문서** → 형상·구조·핵심 규칙 파악.
2. **방법론**(이 repo 안):
   - [`pm_role.md`](.project_manager/wiki/pm_role.md) — 정적 운영 매뉴얼.
   - [`pm_playbook.md`](.project_manager/wiki/pm_playbook.md) — 위임·하니스 플레이북.
   - 엔진/보드 도구: `python3 .project_manager/tools/board.py list`.
3. **dev-state(architecture·ADR·roadmap·board·spike)는 ② PM 홈 `project_manager_dev` 가 소유** — 이
   worktree 의 `.project_manager/wiki/` 에는 없다. 현재-진실 아키텍처/결정이 필요하면 ② repo
   `.project_manager/wiki/` 의 `architecture.md`(현재-아키텍처 단일 진실·ADR 은 *왜*의 히스토리·충돌 시
   architecture 기준)·`decisions/`(ADR — 상한·목록은 색인 `decisions/README.md`)·`roadmap.md` 를 본다. 이 worktree 단독 작업은 엔진
   구조·`tests/`·`templates/` 로 한정한다.

## 구조

```
.project_manager/
  tools/               # canonical 엔진 *.py (board·ticket_finish·pm_*·domain·worktree_pool·additional_reviewer) + engine.manifest
  wiki/                # 방법론 (pm_role.md·pm_playbook.md·_template·domain/)  ← dev-state(architecture·ADR·roadmap·board)는 ② 소유
templates/
  claude_code/         # 출하 Claude Code 템플릿 (엔진 사본 + .claude 어댑터 + CLAUDE.md)
  codex/               # 출하 Codex 템플릿 (엔진 사본 + .codex/.agents 어댑터 + AGENTS.md)
  opencode/            # 출하 opencode 템플릿 (엔진 사본 + .opencode 어댑터 + AGENTS.md)
tests/                 # 엔진 단위테스트 (pytest)
```

## 핵심 규칙 (반드시)

- **엔진은 이 공개 제품 repo(① worktree `work/project_manager_1`)에서 고친다.** `board.py`·`ticket_finish.py`·
  `pm_*.py`·`additional_reviewer.py` 등 엔진 코드 + `wiki/` 방법론·`_template` 의 **canonical 단일 진실 = 이 repo**.
  "고치는 곳 = 테스트하는 곳" — 여기서 고치고 `tests/` 로 검증한다.
- **`templates/*/` 의 엔진을 직접 고치지 마라.** 거긴 이 제품 repo(① worktree)에서 동기화된 사본이다 —
  엔진 변경 후 `pm_update.py --all-targets` 로 `templates/` 아래에 **존재하는 모든 타깃**에 내보낸다
  (제품 repo→템플릿). 단일 타깃만 의도적으로 갱신할 때만 `--target <name>` 을 쓴다.
- **타깃별로 다른 건 어댑터층뿐** — claude=`.claude/`(agents·skills)+`CLAUDE.md` /
  codex=`.codex/`+`.agents/skills`+`AGENTS.md` / opencode=`.opencode/`(agents·command)+`AGENTS.md`.
  엔진은 공유.
- **설계 산출(ADR·roadmap·spike)은 ② PM 홈 `.project_manager/wiki/`**(decisions/·raw/spikes/·roadmap)에
  둔다 — 이 제품 repo 의 wiki 는 엔진 방법론(pm_role·pm_playbook·_template·domain)만. `/spike-new` 로 설계
  spike 박제는 ② 에서.
- **테스트 없이는 끝난 게 아니다.** `python3 -m pytest tests/ -q -n 8`(pytest-xdist 병렬) 통과가
  완료 조건. `-n` 없는 단일 실행도 같은 결과지만 30분+ 걸린다.
- **작은 단위 분할 → 단계별 검증 · 최소 변경 · 명시적 풀네임.**

## 의존성 설치

엔진의 Python 지원 하한은 **3.11**이다(`tomllib` 표준 라이브러리가 지배 제약).

```bash
python3 -m pip install -r requirements-dev.txt   # PyYAML(런타임) + pytest·pytest-xdist(테스트)
```

`requirements.txt` = 런타임(`PyYAML>=6` — board/ticket 도구가 frontmatter 파싱),
`requirements-dev.txt` = 그 위에 `pytest`·`pytest-xdist`(회귀 병렬 실행). fresh clone 은 이걸 먼저
깔아야 import 단계를 넘는다.

## 자주 쓰는 명령

> **Windows 노트:** `python3`/`python` 은 Windows 에서 WindowsApps 가짜 shim(Git Bash 에선
> Permission denied)일 수 있다 — **런처 `py`(예: `py -3.12`)를 1순위로** 쓴다. Linux/macOS 는
> `python3` 그대로. 인코딩은 **엔진이 코드로 처리**(파일 IO `encoding="utf-8"`·콘솔 stdout
> reconfigure)하므로 env prefix 없이 동작한다. cp949 콘솔·외부 파이프에서 드물게 깨지면 **각 셸
> 문법으로** 붙인다 — PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. (bash 전용 아님.)
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다.

```bash
python3 -m pytest tests/ -q -n 8                          # 엔진 테스트 (병렬 · Windows: py -3.12 -m pytest ...)
# `-n 8` 은 워커 수 — 코어 수에 맞춰 조절하고, 생략하면 단일 프로세스로 같은 스위트를 돈다.
python3 .project_manager/tools/board.py list              # 보드
python3 .project_manager/tools/board.py lint              # 의존성·thin·wikilink 검사
# 엔진 변경을 존재하는 모든 타깃에 내보내기 (이 제품 repo → templates/*):
#   python3 .project_manager/tools/pm_update.py --from . --all-targets --dry-run
# 단일 타깃만 의도적으로 갱신할 때: --target <name>
# 외부 프로젝트로 import (루트 파사드 — deep 경로·인터프리터 캡슐화·cwd 무관):
#   <manager>/pm-import.sh --new <dest> --harness opencode   (Windows: pm-import.cmd)
#   — pm_import.py 로 인자 verbatim forward. --from 은 자동으로 manager 루트로 해소(생략 가능).
```

## 후속 (roadmap 단일 진실)

후속 작업의 단일 진실은 PM 홈(`project_manager_dev`)의 `.project_manager/wiki/roadmap.md` 다 —
**여기에 목록을 복제하지 않는다**(복제본은 반드시 drift 한다).
